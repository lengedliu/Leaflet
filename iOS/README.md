# 拾阅 iOS App

这是原生 SwiftUI 工程，最低支持 iOS 17。

## 在 Mac 上运行

1. 将仓库放到装有 Xcode 15 或更新版本的 Mac 上。
2. 打开 `PagesBetween.xcodeproj`。
3. 选择 `PagesBetween` scheme 和 iPhone 模拟器或已连接设备，然后按 Run。
4. 点右上角连接图标，填入 Calibre-Web 的 OPDS 地址及账号。

首版支持 OPDS Atom 书目解析、书名/作者搜索、Keychain 凭据保存、封面显示、EPUB 下载和 Quick Look 预览。服务器须使用 HTTPS（推荐），并在账号验证外允许 iOS 设备访问。阅读进度回传与 OPDS 分页加载尚未实现。
