import SwiftUI
import Security
import QuickLook

@main
struct PagesBetweenApp: App {
    @StateObject private var library = LibraryStore()

    var body: some Scene {
        WindowGroup {
            LibraryView()
                .environmentObject(library)
                .tint(Color(red: 0.14, green: 0.30, blue: 0.24))
        }
    }
}

struct OPDSBook: Identifiable, Hashable {
    let id: String
    let title: String
    let author: String
    let coverURL: URL?
    let downloadURL: URL?
    let summary: String
}

private struct OPDSLink {
    let href: String
    let rel: String
    let type: String
}

private struct OPDSEntry {
    var id = ""
    var title = ""
    var author = ""
    var summary = ""
    var links: [OPDSLink] = []
}

private final class OPDSParser: NSObject, XMLParserDelegate {
    private(set) var entries: [OPDSEntry] = []
    private var entry: OPDSEntry?
    private var element = ""
    private var capture = ""
    private var authorDepth = 0

    func parser(_ parser: XMLParser, didStartElement name: String, namespaceURI: String?, qualifiedName qName: String?, attributes: [String: String] = [:]) {
        let key = (qName ?? name).split(separator: ":").last.map(String.init) ?? name
        element = key
        if key == "entry" { entry = OPDSEntry() }
        if key == "author" { authorDepth += 1 }
        if key == "link", entry != nil, let href = attributes["href"] {
            entry?.links.append(OPDSLink(href: href, rel: attributes["rel"] ?? "", type: attributes["type"] ?? ""))
        }
        if key == "id" || key == "title" || key == "name" || key == "summary" { capture = key }
    }

    func parser(_ parser: XMLParser, foundCharacters string: String) {
        guard !capture.isEmpty else { return }
        switch capture {
        case "id": entry?.id += string
        case "title": entry?.title += string
        case "name": if authorDepth > 0 { entry?.author += string }
        case "summary": entry?.summary += string
        default: break
        }
    }

    func parser(_ parser: XMLParser, didEndElement name: String, namespaceURI: String?, qualifiedName qName: String?) {
        let key = (qName ?? name).split(separator: ":").last.map(String.init) ?? name
        if key == "author" { authorDepth = max(0, authorDepth - 1) }
        if key == capture { capture = "" }
        if key == "entry", let entry {
            entries.append(entry)
            self.entry = nil
        }
        element = ""
    }
}

@MainActor
final class LibraryStore: ObservableObject {
    @Published private(set) var books: [OPDSBook] = []
    @Published private(set) var isLoading = false
    @Published var errorMessage: String?
    @Published var connected = false
    @Published var serverAddress = UserDefaults.standard.string(forKey: "opdsServerAddress") ?? ""
    @Published var searchText = ""

    private let credentialKey = "calibre-web-opds"
    private var credentials: (String, String) { KeychainCredentials.read(key: credentialKey) ?? ("", "") }
    var filteredBooks: [OPDSBook] {
        let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else { return books }
        return books.filter { $0.title.localizedCaseInsensitiveContains(query) || $0.author.localizedCaseInsensitiveContains(query) }
    }

    func connect(address: String, username: String, password: String) async {
        guard let url = URL(string: address.trimmingCharacters(in: .whitespacesAndNewlines)),
              let scheme = url.scheme?.lowercased(), ["http", "https"].contains(scheme), url.host != nil else {
            errorMessage = "请输入有效的 http 或 https OPDS 地址。"
            return
        }
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            let feed = try await fetchFeed(at: url, username: username, password: password)
            let parser = OPDSParser()
            let xml = XMLParser(data: feed)
            xml.delegate = parser
            guard xml.parse() else { throw LibraryError.invalidFeed }
            let parsed = parser.entries.map { entry -> OPDSBook in
                func absolute(_ href: String) -> URL? { URL(string: href, relativeTo: url)?.absoluteURL }
                let acquisition = entry.links.first { $0.rel.localizedCaseInsensitiveContains("acquisition") || $0.type.localizedCaseInsensitiveContains("epub") || $0.type.localizedCaseInsensitiveContains("ebook") }
                let cover = entry.links.first { $0.rel.localizedCaseInsensitiveContains("image") || $0.rel.localizedCaseInsensitiveContains("thumbnail") || $0.type.localizedCaseInsensitiveContains("image") }
                let stableID = entry.id.isEmpty ? entry.title + entry.author : entry.id
                return OPDSBook(id: stableID, title: entry.title.isEmpty ? "未命名书籍" : entry.title,
                                author: entry.author.isEmpty ? "未知作者" : entry.author,
                                coverURL: cover.flatMap { absolute($0.href) }, downloadURL: acquisition.flatMap { absolute($0.href) },
                                summary: entry.summary)
            }
            guard !parsed.isEmpty else { throw LibraryError.emptyFeed }
            books = parsed
            connected = true
            serverAddress = url.absoluteString
            UserDefaults.standard.set(serverAddress, forKey: "opdsServerAddress")
            try KeychainCredentials.save(username: username, password: password, key: credentialKey)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func refresh() async {
        let account = credentials
        await connect(address: serverAddress, username: account.0, password: account.1)
    }

    func download(_ book: OPDSBook) async throws -> URL {
        guard let url = book.downloadURL else { throw LibraryError.noDownload }
        var request = URLRequest(url: url)
        let account = credentials
        if !account.0.isEmpty || !account.1.isEmpty {
            let token = Data("\(account.0):\(account.1)".utf8).base64EncodedString()
            request.setValue("Basic \(token)", forHTTPHeaderField: "Authorization")
        }
        let (temporaryURL, response) = try await URLSession.shared.download(for: request)
        if let response = response as? HTTPURLResponse, !(200..<300).contains(response.statusCode) {
            throw LibraryError.http(response.statusCode)
        }
        let ext = url.pathExtension.isEmpty ? "epub" : url.pathExtension
        let safeName = book.title.replacingOccurrences(of: "/", with: "-")
        let destination = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("\(safeName).\(ext)")
        try? FileManager.default.removeItem(at: destination)
        try FileManager.default.moveItem(at: temporaryURL, to: destination)
        return destination
    }

    private func fetchFeed(at url: URL, username: String, password: String) async throws -> Data {
        var request = URLRequest(url: url, timeoutInterval: 30)
        request.setValue("application/atom+xml, application/xml, text/xml", forHTTPHeaderField: "Accept")
        if !username.isEmpty || !password.isEmpty {
            request.setValue("Basic \(Data("\(username):\(password)".utf8).base64EncodedString())", forHTTPHeaderField: "Authorization")
        }
        let (data, response) = try await URLSession.shared.data(for: request)
        if let response = response as? HTTPURLResponse, !(200..<300).contains(response.statusCode) {
            throw LibraryError.http(response.statusCode)
        }
        return data
    }
}

private enum LibraryError: LocalizedError {
    case invalidFeed, emptyFeed, noDownload, http(Int)
    var errorDescription: String? {
        switch self {
        case .invalidFeed: "无法解析 OPDS XML 目录。"
        case .emptyFeed: "目录中暂时没有可显示的书籍。"
        case .noDownload: "这本书没有提供可下载的链接。"
        case .http(let code): "服务器返回 HTTP \(code)。请检查地址和登录信息。"
        }
    }
}

private enum KeychainCredentials {
    static func read(key: String) -> (String, String)? {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: key, kSecReturnData as String: true]
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data,
              let value = try? JSONDecoder().decode([String: String].self, from: data) else { return nil }
        return (value["username"] ?? "", value["password"] ?? "")
    }

    static func save(username: String, password: String, key: String) throws {
        let data = try JSONEncoder().encode(["username": username, "password": password])
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: key]
        let values: [String: Any] = [kSecValueData as String: data, kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
        let status = SecItemUpdate(query as CFDictionary, values as CFDictionary)
        if status == errSecItemNotFound {
            var insert = query
            values.forEach { insert[$0.key] = $0.value }
            let result = SecItemAdd(insert as CFDictionary, nil)
            guard result == errSecSuccess else { throw KeychainError.status(result) }
        } else if status != errSecSuccess { throw KeychainError.status(status) }
    }
}

private enum KeychainError: LocalizedError {
    case status(OSStatus)
    var errorDescription: String? { if case .status(let status) = self { return "无法安全保存登录信息（\(status)）。" }; return "Keychain 错误。" }
}

struct LibraryView: View {
    @EnvironmentObject private var library: LibraryStore
    @State private var showingConnection = false
    @State private var selectedBook: OPDSBook?
    @State private var downloadedURL: URL?
    @State private var showingPreview = false
    @State private var downloadingID: String?
    @State private var downloadError: String?
    private let columns = [GridItem(.adaptive(minimum: 104), spacing: 17, alignment: .top)]

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    header
                    if !library.books.isEmpty { continueReading }
                    HStack(alignment: .firstTextBaseline) {
                        Text(library.searchText.isEmpty ? "我的书库" : "搜索结果").font(.system(size: 21, weight: .semibold, design: .serif))
                        Spacer()
                        Text("\(library.filteredBooks.count) 本").font(.caption).foregroundStyle(.secondary)
                    }.padding(.top, 2)
                    if library.isLoading && library.books.isEmpty {
                        ProgressView("正在连接书库…").frame(maxWidth: .infinity, minHeight: 160)
                    } else if library.filteredBooks.isEmpty {
                        emptyState
                    } else {
                        LazyVGrid(columns: columns, alignment: .leading, spacing: 22) {
                            ForEach(library.filteredBooks) { book in
                                Button { selectedBook = book } label: { BookTile(book: book) }
                                    .buttonStyle(.plain)
                            }
                        }
                    }
                }
                .padding(.horizontal, 20)
                .padding(.top, 12)
                .padding(.bottom, 34)
            }
            .background(Color(red: 0.965, green: 0.96, blue: 0.94))
            .toolbar(.hidden, for: .navigationBar)
            .searchable(text: $library.searchText, prompt: "搜索书名或作者")
            .refreshable { await library.refresh() }
            .sheet(isPresented: $showingConnection) { ConnectionSheet() }
            .sheet(item: $selectedBook) { book in BookDetailSheet(book: book, onRead: { Task { await open(book) } }) }
            .sheet(isPresented: $showingPreview) { if let url = downloadedURL { EPUBPreview(url: url).ignoresSafeArea() } }
            .alert("无法打开书籍", isPresented: Binding(get: { downloadError != nil }, set: { if !$0 { downloadError = nil } })) {
                Button("好", role: .cancel) { downloadError = nil }
            } message: { Text(downloadError ?? "") }
        }
        .preferredColorScheme(.light)
    }

    private var header: some View {
        HStack(alignment: .center) {
            HStack(spacing: 10) {
                Image(systemName: "book.closed.fill").font(.system(size: 15)).foregroundStyle(.white).frame(width: 34, height: 34).background(Color(red: 0.14, green: 0.30, blue: 0.24), in: RoundedRectangle(cornerRadius: 11))
                VStack(alignment: .leading, spacing: 2) {
                    Text("拾阅").font(.system(size: 18, weight: .bold, design: .serif))
                    HStack(spacing: 5) { Circle().fill(library.connected ? .green : .gray.opacity(0.45)).frame(width: 6, height: 6); Text(library.connected ? "书库已连接" : "私人书库阅读器").font(.system(size: 10)).foregroundStyle(.secondary) }
                }
            }
            Spacer()
            Button { showingConnection = true } label: {
                Image(systemName: library.connected ? "arrow.clockwise" : "link").font(.system(size: 15, weight: .medium)).foregroundStyle(Color(red: 0.14, green: 0.30, blue: 0.24)).frame(width: 39, height: 39).background(.white.opacity(0.8), in: Circle())
            }.accessibilityLabel(library.connected ? "同步书库" : "连接书库")
        }
    }

    private var continueReading: some View {
        HStack(spacing: 14) {
            Image(systemName: "text.book.closed.fill").font(.system(size: 25)).foregroundStyle(Color(red: 0.36, green: 0.47, blue: 0.39)).frame(width: 66, height: 83).background(Color(red: 0.88, green: 0.89, blue: 0.84), in: RoundedRectangle(cornerRadius: 8))
            VStack(alignment: .leading, spacing: 5) {
                Text("随时开始阅读").font(.system(size: 10, weight: .medium)).tracking(1).foregroundStyle(.secondary)
                Text("连接你的 Calibre-Web").font(.system(size: 15, weight: .semibold, design: .serif))
                Text("书籍与阅读进度由你掌控").font(.system(size: 11)).foregroundStyle(.secondary)
                Button("连接 OPDS 书库  →") { showingConnection = true }.font(.system(size: 11, weight: .semibold)).padding(.top, 3)
            }
            Spacer(minLength: 0)
        }
        .padding(12).frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(red: 0.92, green: 0.92, blue: 0.89), in: RoundedRectangle(cornerRadius: 15))
    }

    private var emptyState: some View {
        VStack(spacing: 12) {
            Image(systemName: "books.vertical").font(.system(size: 29, weight: .light)).foregroundStyle(.secondary)
            Text(library.connected ? "没有找到这本书" : "让你的书库连上拾阅").font(.system(size: 16, weight: .medium, design: .serif))
            Text(library.errorMessage ?? (library.connected ? "试试其他书名或作者。" : "输入 Calibre-Web 的 OPDS 地址，开始阅读你的藏书。"))
                .font(.system(size: 12)).foregroundColor(library.errorMessage == nil ? Color.secondary : Color.red).multilineTextAlignment(.center)
            if !library.connected { Button("连接书库") { showingConnection = true }.font(.system(size: 13, weight: .semibold)).padding(.top, 2) }
        }.frame(maxWidth: .infinity).padding(.vertical, 45).padding(.horizontal, 20)
    }

    private func open(_ book: OPDSBook) async {
        guard downloadingID == nil else { return }
        downloadingID = book.id
        defer { downloadingID = nil }
        do { downloadedURL = try await library.download(book); showingPreview = true }
        catch { downloadError = error.localizedDescription }
    }
}

private struct BookTile: View {
    let book: OPDSBook
    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            AsyncImage(url: book.coverURL) { phase in
                if let image = phase.image { image.resizable().scaledToFill() }
                else { ZStack { Color(red: 0.89, green: 0.88, blue: 0.84); Image(systemName: "book.closed").font(.system(size: 25, weight: .light)).foregroundStyle(.white.opacity(0.9)) } }
            }
            .frame(maxWidth: .infinity).aspectRatio(0.70, contentMode: .fit).clipShape(RoundedRectangle(cornerRadius: 6))
            .shadow(color: .black.opacity(0.10), radius: 6, x: 0, y: 4)
            Text(book.title).font(.system(size: 12, weight: .medium, design: .serif)).foregroundStyle(Color.primary).lineLimit(2).multilineTextAlignment(.leading)
            Text(book.author).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
        }
    }
}

private struct ConnectionSheet: View {
    @EnvironmentObject private var library: LibraryStore
    @Environment(\.dismiss) private var dismiss
    @State private var address = ""
    @State private var username = ""
    @State private var password = ""

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("https://books.example.com/opds", text: $address).keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled().textContentType(.URL)
                    TextField("用户名", text: $username).textContentType(.username).textInputAutocapitalization(.never).autocorrectionDisabled()
                    SecureField("密码", text: $password).textContentType(.password)
                } header: { Text("Calibre-Web OPDS") } footer: { Text("地址一般以 /opds 或 /opds/v1.2 结尾。登录信息只保存在本机 Keychain。") }
                if let error = library.errorMessage { Section { Text(error).font(.footnote).foregroundStyle(.red) } }
                Section { Button { Task { await library.connect(address: address, username: username, password: password); if library.connected { dismiss() } } } label: { HStack { Spacer(); if library.isLoading { ProgressView().padding(.trailing, 6) }; Text(library.isLoading ? "正在连接…" : "连接书库"); Spacer() } }.disabled(library.isLoading) }
            }
            .navigationTitle("连接书库")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .topBarTrailing) { Button("完成") { dismiss() } } }
            .onAppear { address = library.serverAddress }
        }
        .presentationDetents([.medium, .large])
    }
}

private struct BookDetailSheet: View {
    let book: OPDSBook
    let onRead: () -> Void
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        VStack(spacing: 18) {
            AsyncImage(url: book.coverURL) { phase in
                if let image = phase.image { image.resizable().scaledToFill() }
                else { ZStack { Color(red: 0.89, green: 0.88, blue: 0.84); Image(systemName: "book.closed").font(.largeTitle).foregroundStyle(.white) } }
            }.frame(width: 142, height: 204).clipShape(RoundedRectangle(cornerRadius: 8)).shadow(color: .black.opacity(0.16), radius: 12, y: 7)
            VStack(spacing: 5) { Text(book.title).font(.system(size: 20, weight: .medium, design: .serif)).multilineTextAlignment(.center); Text(book.author).font(.system(size: 13)).foregroundStyle(.secondary) }
            if !book.summary.isEmpty { Text(book.summary).font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(4).multilineTextAlignment(.center).padding(.horizontal, 18) }
            Button { dismiss(); onRead() } label: { Label("下载并阅读", systemImage: "arrow.down.to.line.compact").font(.system(size: 14, weight: .semibold)).frame(maxWidth: .infinity).padding(.vertical, 14).foregroundStyle(.white).background(Color(red: 0.14, green: 0.30, blue: 0.24), in: RoundedRectangle(cornerRadius: 13)) }.padding(.top, 3)
                .disabled(book.downloadURL == nil).opacity(book.downloadURL == nil ? 0.45 : 1)
        }
        .padding(24).frame(maxWidth: .infinity, maxHeight: .infinity).background(Color(red: 0.965, green: 0.96, blue: 0.94))
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
    }
}

private struct EPUBPreview: UIViewControllerRepresentable {
    let url: URL
    func makeCoordinator() -> Coordinator { Coordinator(url: url) }
    func makeUIViewController(context: Context) -> QLPreviewController { let controller = QLPreviewController(); controller.dataSource = context.coordinator; return controller }
    func updateUIViewController(_ controller: QLPreviewController, context: Context) { context.coordinator.url = url; controller.reloadData() }
    final class Coordinator: NSObject, QLPreviewControllerDataSource {
        var url: URL
        init(url: URL) { self.url = url }
        func numberOfPreviewItems(in controller: QLPreviewController) -> Int { 1 }
        func previewController(_ controller: QLPreviewController, previewItemAt index: Int) -> QLPreviewItem { url as NSURL }
    }
}
