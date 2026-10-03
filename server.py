#!/usr/bin/env python3
"""Small same-origin OPDS bridge and static server for the Pages Between PWA."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, urljoin, parse_qs
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.error import HTTPError, URLError
from xml.etree import ElementTree
from html.parser import HTMLParser
from pathlib import Path
import base64
import gzip
import hmac
import hashlib
import json
import html
import heapq
import mimetypes
import os
import secrets
import re
import time
import threading
import uuid
import zipfile
from collections import OrderedDict

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "8080"))
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() in ("1", "true", "yes")
HOST = os.environ.get("HOST", "0.0.0.0" if APP_PASSWORD else "127.0.0.1")
MAX_WORKERS = max(2, int(os.environ.get("MAX_WORKERS", "12")))
CONTENT_PROCESSING_SLOTS = threading.BoundedSemaphore(max(1, min(2, MAX_WORKERS // 2)))
EPUB_DOWNLOAD_SLOTS = threading.BoundedSemaphore(2)
MAX_BOOK_BYTES = 120 * 1024 * 1024
MAX_EPUB_EXPANDED_BYTES = 256 * 1024 * 1024
EPUB_CHAPTER_CACHE_BYTES = 16 * 1024 * 1024
EPUB_CHAPTER_CACHE_ENTRY_BYTES = 512 * 1024
EPUB_CHAPTER_CACHE_ENTRIES = 128
MAX_OPDS_SESSIONS = 8192
MAX_OPDS_SESSION_BYTES = 64 * 1024 * 1024
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", str(ROOT / "config")))
CONFIG_FILE = CONFIG_DIR / "calibre-web.json"
SHELF_FILE = CONFIG_DIR / "bookshelf.json"
HISTORY_FILE = CONFIG_DIR / "history.json"
EPUB_CACHE_DIR = CONFIG_DIR / "epub-cache"
MAX_BODY = 1024 * 1024
sessions = OrderedDict()
session_sizes = {}
session_cache_bytes = 0
sessions_lock = threading.RLock()
auth_sessions = {}
login_failures = {}
recommendation_cache = {}
shelf_lock = threading.RLock()
history_lock = threading.RLock()
config_lock = threading.RLock()
epub_cache = {}
epub_spines = {}
epub_cache_lock = threading.RLock()
epub_chapter_cache = OrderedDict()
epub_chapter_cache_size = 0
epub_chapter_cache_lock = threading.RLock()
epub_prefetches = set()
epub_prefetch_lock = threading.Lock()


class RemoteRangeUnsupported(ValueError):
    pass


class RemoteRangeFile:
    """Small seekable adapter that reads ZIP data using authenticated HTTP ranges."""
    def __init__(self, item):
        self.item = item
        self.position = 0
        self.closed = False
        try:
            with fetch(item["download"], item["username"], item["password"], extra_headers={"Range": "bytes=0-0"}) as response:
                content_range = response.headers.get("Content-Range", "")
                match = re.fullmatch(r"bytes 0-0/(\d+)", content_range.strip())
                if getattr(response, "status", 200) != 206 or not match or len(response.read(2)) != 1:
                    raise RemoteRangeUnsupported("书库不支持 EPUB 随机读取。")
                self.size = int(match.group(1))
        except HTTPError as error:
            if error.code in (400, 405, 416, 501):
                raise RemoteRangeUnsupported("书库不支持 EPUB 随机读取。") from error
            raise

    def seek(self, offset, whence=0):
        if whence == 0:
            position = offset
        elif whence == 1:
            position = self.position + offset
        elif whence == 2:
            position = self.size + offset
        else:
            raise ValueError("无效的 ZIP 文件偏移方式。")
        if position < 0:
            raise ValueError("无效的 ZIP 文件偏移位置。")
        self.position = min(position, self.size)
        return self.position

    def tell(self):
        return self.position

    def seekable(self):
        return True

    def readable(self):
        return True

    def read(self, size=-1):
        if self.closed:
            raise ValueError("远程 EPUB 已关闭。")
        if size is None or size < 0:
            size = self.size - self.position
        size = min(size, self.size - self.position)
        if size <= 0:
            return b""
        start, end = self.position, self.position + size - 1
        try:
            with fetch(self.item["download"], self.item["username"], self.item["password"],
                       extra_headers={"Range": f"bytes={start}-{end}"}) as response:
                content_range = response.headers.get("Content-Range", "")
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range.strip())
                if (getattr(response, "status", 200) != 206 or not match or
                        tuple(map(int, match.groups())) != (start, end, self.size)):
                    raise RemoteRangeUnsupported("书库未能按范围返回 EPUB 数据。")
                data = response.read(size + 1)
        except HTTPError as error:
            if error.code in (400, 405, 416, 501):
                raise RemoteRangeUnsupported("书库未能按范围返回 EPUB 数据。") from error
            raise
        if len(data) != size:
            raise RemoteRangeUnsupported("书库返回的 EPUB 数据范围不完整。")
        self.position += size
        return data

    def close(self):
        self.closed = True


def get_opds_session(key, default=None):
    global session_cache_bytes
    now = time.time()
    with sessions_lock:
        value = sessions.get(key)
        if value is None:
            return default
        if value.get("expires", 0) <= now:
            sessions.pop(key, None)
            session_cache_bytes -= session_sizes.pop(key, 0)
            return default
        sessions.move_to_end(key)
        return value


def put_opds_session(key, value):
    global session_cache_bytes
    with sessions_lock:
        previous = sessions.pop(key, None)
        if previous is not None:
            session_cache_bytes -= session_sizes.pop(key, 0)
        sessions[key] = value
        size = sum(len(field.encode("utf-8")) for field in value.values() if isinstance(field, str))
        session_sizes[key] = size
        session_cache_bytes += size
        sessions.move_to_end(key)
        while len(sessions) > MAX_OPDS_SESSIONS or session_cache_bytes > MAX_OPDS_SESSION_BYTES:
            for candidate in list(sessions):
                if candidate[1] == "root":
                    sessions.move_to_end(candidate)
                    continue
                sessions.pop(candidate, None)
                session_cache_bytes -= session_sizes.pop(candidate, 0)
                break
            else:
                candidate, _ = sessions.popitem(last=False)
                session_cache_bytes -= session_sizes.pop(candidate, 0)


def prune_opds_sessions(now=None):
    global session_cache_bytes
    now = time.time() if now is None else now
    with sessions_lock:
        for key, value in list(sessions.items()):
            if value.get("expires", 0) <= now:
                sessions.pop(key, None)
                session_cache_bytes -= session_sizes.pop(key, 0)


def clear_epub_chapter_cache(path):
    global epub_chapter_cache_size
    key_path = str(path)
    with epub_chapter_cache_lock:
        for key in [key for key in epub_chapter_cache if key[0] == key_path]:
            cached = epub_chapter_cache.pop(key)
            epub_chapter_cache_size -= cached[1]


def remember_epub_chapter(path, chapter_index, content, chapter_count):
    global epub_chapter_cache_size
    size = len(content.encode("utf-8"))
    if size == 0 or size > EPUB_CHAPTER_CACHE_ENTRY_BYTES:
        return
    key = (str(path), chapter_index)
    with epub_chapter_cache_lock:
        previous = epub_chapter_cache.pop(key, None)
        if previous:
            epub_chapter_cache_size -= previous[1]
        epub_chapter_cache[key] = (content, size, chapter_count)
        epub_chapter_cache_size += size
        while (epub_chapter_cache_size > EPUB_CHAPTER_CACHE_BYTES or
               len(epub_chapter_cache) > EPUB_CHAPTER_CACHE_ENTRIES) and epub_chapter_cache:
            _, removed = epub_chapter_cache.popitem(last=False)
            epub_chapter_cache_size -= removed[1]

if APP_PASSWORD and len(APP_PASSWORD) < 12:
    raise ValueError("APP_PASSWORD 至少需要 12 个字符。")
if HOST in ("0.0.0.0", "::") and not APP_PASSWORD:
    raise ValueError("监听所有网络接口时必须设置 APP_PASSWORD。")


def origin_of(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("书库链接必须使用有效的 HTTP 或 HTTPS 地址。")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme.lower(), parsed.hostname.lower(), port


def library_identity(url):
    parsed = urlparse(url)
    scheme, host, port = origin_of(url)
    default_port = 443 if scheme == "https" else 80
    netloc = host if port == default_port else f"{host}:{port}"
    path = parsed.path.rstrip("/") or "/"
    return f"{scheme}://{netloc}{path}"


def same_origin_url(base, href):
    target = urljoin(base, href)
    if origin_of(target) != origin_of(base):
        raise ValueError("Calibre-Web 目录包含跨站链接，已阻止访问。")
    return target


def library_ids(url):
    scheme, host, port = origin_of(url)
    default_port = 443 if scheme == "https" else 80
    netloc = host if port == default_port else f"{host}:{port}"
    return {f"{scheme}://{netloc}", library_identity(url)}


def shelf_key(download, title, author):
    identity = "\0".join((download, title, author)).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()[:32]


def load_bookshelf():
    if not SHELF_FILE.is_file():
        return []
    data = json.loads(SHELF_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        return []
    # Ignore malformed records from manual edits or interrupted legacy writes.
    return [record for record in data if isinstance(record, dict)
            and isinstance(record.get("key"), str)
            and isinstance(record.get("library"), str)
            and isinstance(record.get("download"), str)]


def save_bookshelf(books):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_DIR / f".bookshelf.{uuid.uuid4().hex}.tmp"
    temporary.write_text(json.dumps(books, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, SHELF_FILE)


def load_reading_history():
    with history_lock:
        if not HISTORY_FILE.is_file():
            return []
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        return []
    records = []
    for record in data:
        if not isinstance(record, dict) or not isinstance(record.get("key"), str) \
                or not isinstance(record.get("library"), str) or not isinstance(record.get("download"), str):
            continue
        try:
            record["lastRead"] = max(0, int(record.get("lastRead", 0)))
        except (TypeError, ValueError):
            record["lastRead"] = 0
        try:
            record["lastViewed"] = max(0, int(record.get("lastViewed", 0)))
        except (TypeError, ValueError):
            record["lastViewed"] = 0
        records.append(record)
    return records


def save_reading_history(records):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_DIR / f".history.{uuid.uuid4().hex}.tmp"
    with history_lock:
        temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, HISTORY_FILE)


def save_connection_config(address, username, password):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_DIR / f".calibre-web.{uuid.uuid4().hex}.tmp"
    with config_lock:
        temporary.write_text(json.dumps({"address": address, "username": username, "password": password}, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, CONFIG_FILE)


def delete_connection_config():
    with config_lock:
        try:
            CONFIG_FILE.unlink()
        except FileNotFoundError:
            pass


def load_connection_config():
    with config_lock:
        if not CONFIG_FILE.is_file():
            return None
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def local_name(tag):
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def child_text(node, name):
    for child in node.iter():
        if local_name(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def child_description(node):
    class PlainText(HTMLParser):
        blocks = {"p", "div", "section", "li", "br", "h1", "h2", "h3", "h4"}

        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []
            self.skip = 0

        def handle_starttag(self, tag, attrs):
            tag = tag.lower()
            if tag in ("script", "style"):
                self.skip += 1
            elif not self.skip and tag in self.blocks:
                self.parts.append("\n")

        def handle_endtag(self, tag):
            tag = tag.lower()
            if tag in ("script", "style") and self.skip:
                self.skip -= 1
            elif not self.skip and tag in self.blocks:
                self.parts.append("\n")

        def handle_data(self, data):
            if not self.skip:
                self.parts.append(data)

    def markup(element):
        parts = [element.text or ""]
        for child in element:
            tag = local_name(child.tag)
            parts.append(f"<{tag}>{markup(child)}</{tag}>")
            parts.append(child.tail or "")
        return "".join(parts)

    for child in node:
        if local_name(child.tag) not in ("summary", "content", "description"):
            continue
        parser = PlainText()
        parser.feed(markup(child))
        lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
        result = "\n".join(line for line in lines if line).strip()
        if result:
            return result[:10000]
    return ""


def fetch(url, username="", password="", accept="*/*", extra_headers=None, timeout=25):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("请输入有效的 HTTP 或 HTTPS 地址（不要把账号密码写进 URL）。")
    headers = {"Accept": accept, "User-Agent": "PagesBetween/1.0"}
    for name, value in (extra_headers or {}).items():
        if name.lower() in ("range", "if-range") and "\r" not in value and "\n" not in value:
            headers[name] = value
    if username or password:
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers["Authorization"] = "Basic " + token
    request = Request(url, headers=headers)
    allowed_origin = origin_of(url)

    class SameOriginRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, response_headers, newurl):
            if origin_of(newurl) != allowed_origin:
                raise ValueError("Calibre-Web 将请求重定向到其他站点，已阻止访问。")
            return super().redirect_request(req, fp, code, msg, response_headers, newurl)

    return build_opener(SameOriginRedirect()).open(request, timeout=timeout)


class Handler(BaseHTTPRequestHandler):
    server_version = "PagesBetween/1.0"

    def log_message(self, fmt, *args):
        # Keep operational logs useful without printing OPDS credentials or URLs.
        route = urlparse(self.path).path
        print(f"{self.log_date_time_string()} {self.client_address[0]} {route}")

    def send_json(self, status, data, headers=None):
        body = json.dumps(data, ensure_ascii=False).encode()
        compressed = "gzip" in self.headers.get("Accept-Encoding", "").lower() and len(body) >= 1024
        if compressed:
            body = gzip.compress(body, compresslevel=5)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if compressed:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def app_session(self):
        if not APP_PASSWORD:
            return True
        cookie = self.headers.get("Cookie", "")
        token = next((part.strip().split("=", 1)[1] for part in cookie.split(";")
                      if part.strip().startswith("pages_session=")), "")
        expires = auth_sessions.get(token, 0)
        if expires > time.time():
            return True
        if token:
            auth_sessions.pop(token, None)
        self.send_json(401, {"error": "请先登录阅读器。", "loginRequired": True})
        return False

    def auth_cookie_token(self):
        return next((part.strip().split("=", 1)[1] for part in self.headers.get("Cookie", "").split(";")
                     if part.strip().startswith("pages_session=")), "")

    def same_site_post(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            return urlparse(origin).netloc.lower() == self.headers.get("Host", "").lower()
        except ValueError:
            return False

    def handle_login(self):
        if not APP_PASSWORD:
            self.send_json(200, {"ok": True, "required": False})
            return
        try:
            data = self.read_json()
            password = str(data.get("password", ""))
            if not hmac.compare_digest(password.encode("utf-8"), APP_PASSWORD.encode("utf-8")):
                now = time.time()
                address = self.client_address[0]
                attempts = [attempt for attempt in login_failures.get(address, []) if now - attempt < 60]
                attempts.append(now)
                login_failures[address] = attempts
                if len(attempts) >= 10:
                    self.send_json(429, {"error": "登录尝试过多，请稍后再试。"}, {"Retry-After": "60"})
                    return
                self.send_json(401, {"error": "阅读器密码不正确。"})
                return
            login_failures.pop(self.client_address[0], None)
            token = secrets.token_urlsafe(32)
            expires = time.time() + 12 * 60 * 60
            now = time.time()
            for old_token, old_expiry in list(auth_sessions.items()):
                if old_expiry <= now:
                    auth_sessions.pop(old_token, None)
            auth_sessions[token] = expires
            cookie = f"pages_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200"
            if COOKIE_SECURE:
                cookie += "; Secure"
            self.send_json(200, {"ok": True}, {"Set-Cookie": cookie})
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error) or "登录请求格式无效。"})

    def parse_feed(self, payload, address, username, password, token):
        now = time.time()
        prune_opds_sessions(now)
        for cache_token, (expires, _) in list(recommendation_cache.items()):
            if expires <= now:
                recommendation_cache.pop(cache_token, None)
        root = ElementTree.fromstring(payload)
        entries = []
        for node in root.iter():
            if local_name(node.tag) != "entry":
                continue
            links = [child for child in node if local_name(child.tag) == "link"]
            acquisitions = [link for link in links if "acquisition" in link.attrib.get("rel", "").lower()
                            or "epub" in link.attrib.get("type", "").lower()
                            or "ebook" in link.attrib.get("type", "").lower()
                            or "pdf" in link.attrib.get("type", "").lower()
                            or "text/plain" in link.attrib.get("type", "").lower()]
            def format_of(link):
                hint = (link.attrib.get("type", "") + " " + link.attrib.get("href", "")).lower()
                if "epub" in hint: return "epub"
                if "pdf" in hint: return "pdf"
                if "text/plain" in hint or hint.split("?")[0].endswith(".txt"): return "txt"
                return "other"
            acquisition = next((link for wanted in ("epub", "pdf", "txt") for link in acquisitions if format_of(link) == wanted), None)
            if acquisition is None and acquisitions:
                acquisition = acquisitions[0]
            book_format = format_of(acquisition) if acquisition is not None else ""
            navigation = next((link for link in links if "subsection" in link.attrib.get("rel", "").lower()
                               or ("opds-catalog" in link.attrib.get("type", "").lower() and acquisition is None)), None)
            cover = next((link for link in links if "image" in (link.attrib.get("rel", "") + " " + link.attrib.get("type", "")).lower()
                          or "thumbnail" in link.attrib.get("rel", "").lower()), None)
            item_id = uuid.uuid4().hex
            title = child_text(node, "title") or "未命名书籍"
            author = child_text(node, "name") or "未知作者"
            description = child_description(node)
            download = same_origin_url(address, acquisition.attrib["href"]) if acquisition is not None else ""
            stable_key = shelf_key(download, title, author) if download else ""
            item = {
                "id": item_id,
                "title": title,
                "author": author,
                "description": description,
                "tag": "目录" if navigation is not None else (book_format.upper() if book_format else "OPDS"),
                "category": "all",
                "kind": "navigation" if navigation is not None else "book",
                "format": book_format,
                "shelfKey": stable_key,
                "href": f"/api/opds?token={token}&id={item_id}" if navigation is not None else (f"/api/read?token={token}&id={item_id}" if acquisition is not None and book_format in ("epub", "pdf", "txt") else ""),
                "coverUrl": f"/api/cover?token={token}&id={item_id}" if cover is not None and cover.attrib.get("href") else "",
            }
            entries.append(item)
            put_opds_session((token, item_id), {
                "username": username,
                "password": password,
                "address": address,
                "download": download,
                "cover": same_origin_url(address, cover.attrib["href"]) if cover is not None and cover.attrib.get("href") else "",
                "title": title,
                "author": author,
                "description": description,
                "shelfKey": stable_key,
                "format": book_format,
                "feed": same_origin_url(address, navigation.attrib["href"]) if navigation is not None else "",
                "expires": time.time() + 12 * 60 * 60,
            })
        if not entries:
            raise ValueError("OPDS 目录中没有找到书籍或分类。")
        next_link = next((link for link in root.iter() if local_name(link.tag) == "link"
                          and "next" in link.attrib.get("rel", "").lower()
                          and link.attrib.get("href")), None)
        next_href = ""
        if next_link is not None:
            next_address = same_origin_url(address, next_link.attrib["href"])
            next_id = uuid.uuid4().hex
            put_opds_session((token, next_id), {
                "username": username, "password": password, "address": address,
                "download": "", "cover": "", "title": "下一页", "author": "",
                "description": "", "shelfKey": "", "format": "",
                "feed": next_address, "expires": time.time() + 12 * 60 * 60,
            })
            next_href = f"/api/opds?token={token}&id={next_id}"
        return {
            "books": entries,
            "count": len(entries),
            "title": self.directory_title(address, root),
            "backHref": f"/api/opds?token={token}&id=root",
            "nextHref": next_href,
        }

    @staticmethod
    def directory_title(address, root):
        segments = [part.lower() for part in urlparse(address).path.rstrip("/").split("/") if part]
        names = {"category": "分类", "author": "作者", "publisher": "出版社", "series": "丛书",
                 "formats": "格式", "books": "书籍", "readbooks": "已读书籍", "unreadbooks": "未读书籍"}
        for segment in reversed(segments):
            if segment in names:
                return names[segment]
        return child_text(root, "title") or "我的书库"

    def load_directory(self, address, username, password, token):
        with fetch(address, username, password, "application/atom+xml, application/xml, text/xml") as response:
            payload = response.read(8 * 1024 * 1024 + 1)
        if len(payload) > 8 * 1024 * 1024:
            raise ValueError("OPDS 目录超过 8 MB，请使用分页目录地址。")
        result = self.parse_feed(payload, address, username, password, token)
        path = urlparse(address).path.rstrip("/").lower()
        if path.endswith("/opds/category"):
            all_categories = next((book for book in result["books"]
                                  if book["kind"] == "navigation" and book["title"].strip().lower() in ("全部", "all")), None)
            if all_categories:
                parsed = urlparse(all_categories["href"])
                params = parse_qs(parsed.query)
                target = get_opds_session((token, params.get("id", [""])[0]), {}).get("feed")
                if target:
                    with fetch(target, username, password, "application/atom+xml, application/xml, text/xml") as response:
                        nested_payload = response.read(8 * 1024 * 1024 + 1)
                    if len(nested_payload) > 8 * 1024 * 1024:
                        raise ValueError("分类列表超过 8 MB。")
                    result = self.parse_feed(nested_payload, target, username, password, token)
                    result["title"] = "分类"
        return result

    @staticmethod
    def paged_html(title, content):
        safe_title = html.escape(title)
        return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>{safe_title}</title><style>
        :root{{--paper:#f7f5ef;--ink:#30332e;--footer:#f7f5efed;--rule:#deddd5}}body[data-theme="white"]{{--paper:#fff;--ink:#222;--footer:#fffffff0;--rule:#ddd}}body[data-theme="night"]{{--paper:#202421;--ink:#e5e3dc;--footer:#202421ed;--rule:#414640}}*{{box-sizing:border-box}}html,body{{width:100%;height:100%;margin:0;overflow:hidden}}body{{background:var(--paper);color:var(--ink);font:18px/2.05 Georgia,"Noto Serif SC",serif;transition:background .2s,color .2s}}#pages{{position:absolute;inset:0;width:100vw;height:100dvh;overflow-x:auto;overflow-y:hidden;padding:34px 24px 68px;column-width:calc(100vw - 48px);column-gap:48px;column-fill:auto;scroll-behavior:smooth;scroll-snap-type:x mandatory;overscroll-behavior-x:contain;touch-action:pan-y;scrollbar-width:none}}#pages::-webkit-scrollbar{{display:none}}#pages>*{{break-inside:avoid-column}}.epub-chapter{{break-inside:auto}}.epub-chapter:not([data-chapter="0"]){{break-before:column}}h1,h2,h3{{font-weight:500;line-height:1.5;margin:0 0 1.2em}}h1{{font-size:1.45em}}h2,h3{{margin-top:1.4em}}p{{margin:0 0 1.2em;text-indent:2em}}blockquote{{margin:1.4em 0;padding-left:1em;border-left:2px solid #829182;color:inherit;opacity:.8}}#page-footer{{position:fixed;z-index:2;bottom:0;left:0;right:0;height:52px;padding:0 24px calc(env(safe-area-inset-bottom));display:flex;align-items:center;gap:14px;background:var(--footer);color:var(--ink);opacity:.82;font:11px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;backdrop-filter:blur(12px)}}#progress{{height:2px;flex:1;background:var(--rule)}}#progress i{{display:block;width:0;height:100%;background:#778d7d;transition:width .15s}}@media(max-width:600px){{body{{font-size:17px}}#pages{{padding:28px 22px 64px;column-width:calc(100vw - 44px);column-gap:44px}}#page-footer{{height:calc(46px + env(safe-area-inset-bottom));padding:0 22px env(safe-area-inset-bottom)}}}}
        </style></head><body><main id="pages" aria-label="阅读内容"><h1>{safe_title}</h1>{content}</main><footer id="page-footer"><span id="page-current">1</span><div id="progress"><i></i></div><span id="page-total">1</span></footer><script>
        const pages=document.getElementById('pages'),current=document.getElementById('page-current'),total=document.getElementById('page-total'),bar=document.querySelector('#progress i');let startX=0,startY=0,restoreReady=false,progressFrame=0,lastSentPage=0,lastSentTotal=0;
        function updateProgress(){{const width=pages.clientWidth||1,base=Number(window.virtualBasePage)||0;const localPage=Math.min(Math.ceil(pages.scrollWidth/width)||1,Math.floor((pages.scrollLeft+width*.35)/width)+1);const localCount=Math.max(1,Math.ceil(pages.scrollWidth/width));const page=base+localPage,count=base+localCount;const totalKnown=!window.chapterLoadingEnabled||window.allChaptersLoaded;current.textContent=page;total.textContent=totalKnown?count:'…';bar.style.width=(Math.min(page,count)/count*100)+'%';const reportedTotal=totalKnown?count:0;if(restoreReady&&(page!==lastSentPage||reportedTotal!==lastSentTotal)){{lastSentPage=page;lastSentTotal=reportedTotal;parent.postMessage({{type:'reader-progress',page,total:reportedTotal}},location.origin)}}}}
        function scheduleProgress(){{if(progressFrame)return;progressFrame=requestAnimationFrame(()=>{{progressFrame=0;updateProgress()}})}}
        function turn(direction){{pages.scrollBy({{left:direction*pages.clientWidth,behavior:'smooth'}})}}
        window.addEventListener('message',event=>{{if(event.origin!==location.origin)return;if(event.data?.type==='pages-turn')turn(Math.sign(event.data.direction||0));if(event.data?.type==='reader-restore'){{restoreReady=!window.ensureReaderPage;if(window.ensureReaderPage)window.ensureReaderPage(Number(event.data.page||1));else{{pages.scrollTo({{left:Math.max(0,(Number(event.data.page||1)-1)*pages.clientWidth),behavior:'auto'}});requestAnimationFrame(updateProgress)}}}}if(event.data?.type==='reader-settings'){{document.body.dataset.theme=['paper','white','night'].includes(event.data.theme)?event.data.theme:'paper';document.body.style.fontSize=(18*Math.max(.8,Math.min(1.6,Number(event.data.fontScale)||1)))+'px'}}}});
        pages.addEventListener('touchstart',event=>{{if(event.touches.length===1){{startX=event.touches[0].clientX;startY=event.touches[0].clientY}}}},{{passive:true}});
        pages.addEventListener('touchend',event=>{{if(!event.changedTouches.length)return;const dx=event.changedTouches[0].clientX-startX,dy=event.changedTouches[0].clientY-startY;if(Math.abs(dx)>42&&Math.abs(dx)>Math.abs(dy)*1.2)turn(dx<0?1:-1)}},{{passive:true}});
        pages.addEventListener('click',event=>{{const x=event.clientX/pages.clientWidth;if(x>.82)turn(1);else if(x<.18)turn(-1)}});
        pages.addEventListener('scroll',scheduleProgress,{{passive:true}});window.addEventListener('resize',scheduleProgress);document.addEventListener('keydown',event=>{{if(event.key==='ArrowRight'||event.key==='PageDown')turn(1);if(event.key==='ArrowLeft'||event.key==='PageUp')turn(-1)}});requestAnimationFrame(updateProgress);
        </script></body></html>'''.encode("utf-8")

    @staticmethod
    def cache_epub_from_remote(item, cache_key):
        EPUB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            EPUB_CACHE_DIR.chmod(0o700)
        except OSError:
            pass
        target = EPUB_CACHE_DIR / (uuid.uuid4().hex + ".epub")
        temporary = target.with_suffix(".tmp")
        EPUB_DOWNLOAD_SLOTS.acquire()
        try:
            total = 0
            with fetch(item["download"], item["username"], item["password"]) as response, temporary.open("wb") as output:
                length = response.headers.get("Content-Length")
                if length and int(length) > MAX_BOOK_BYTES:
                    raise ValueError("电子书文件超过 120 MB。")
                while chunk := response.read(64 * 1024):
                    total += len(chunk)
                    if total > MAX_BOOK_BYTES:
                        raise ValueError("电子书文件超过 120 MB。")
                    output.write(chunk)
            if total == 0 or not zipfile.is_zipfile(temporary):
                raise ValueError("Calibre-Web 返回的文件不是有效 EPUB。")
            os.replace(temporary, target)
            try:
                target.chmod(0o600)
            except OSError:
                pass
        finally:
            EPUB_DOWNLOAD_SLOTS.release()
            temporary.unlink(missing_ok=True)
        now = time.time()
        with epub_cache_lock:
            for path in EPUB_CACHE_DIR.glob("*.epub"):
                try:
                    if now - path.stat().st_mtime > 6 * 60 * 60:
                        path.unlink(missing_ok=True)
                        epub_spines.pop(str(path), None)
                        clear_epub_chapter_cache(path)
                except OSError:
                    continue
            for key, (path, expires) in list(epub_cache.items()):
                if expires <= now or not path.is_file():
                    path.unlink(missing_ok=True)
                    epub_cache.pop(key, None)
                    epub_spines.pop(str(path), None)
                    clear_epub_chapter_cache(path)
            epub_cache[cache_key] = (target, now + 6 * 60 * 60)
            cache_files = sorted(EPUB_CACHE_DIR.glob("*.epub"), key=lambda path: path.stat().st_mtime)
            cache_size = sum(path.stat().st_size for path in cache_files)
            for path in cache_files:
                if cache_size <= 512 * 1024 * 1024:
                    break
                if path == target:
                    continue
                size = path.stat().st_size
                path.unlink(missing_ok=True)
                cache_size -= size
                for key, (cached_path, _) in list(epub_cache.items()):
                    if cached_path == path:
                        epub_cache.pop(key, None)
                epub_spines.pop(str(path), None)
                clear_epub_chapter_cache(path)
        return target

    @staticmethod
    def epub_chapter(path, chapter_index, remote_item=None):
        source_key = ("remote:" + hashlib.sha256(remote_item["download"].encode()).hexdigest()) if remote_item else str(path)
        cache_key = (source_key, chapter_index)
        with epub_chapter_cache_lock:
            cached_chapter = epub_chapter_cache.get(cache_key)
            if cached_chapter is not None:
                epub_chapter_cache.move_to_end(cache_key)
                return cached_chapter[0], cached_chapter[2]
        source = RemoteRangeFile(remote_item) if remote_item else path
        with zipfile.ZipFile(source) as archive:
            spine_paths = epub_spines.get(source_key)
            if spine_paths is None:
                entries = archive.infolist()
                if len(entries) > 10000 or sum(entry.file_size for entry in entries) > MAX_EPUB_EXPANDED_BYTES:
                    raise ValueError("EPUB 解压后的内容超过安全限制。")
                container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
                opf_path = next(node.attrib["full-path"] for node in container.iter() if local_name(node.tag) == "rootfile")
                opf = ElementTree.fromstring(archive.read(opf_path))
                base = os.path.dirname(opf_path)
                manifest = {node.attrib.get("id"): node.attrib.get("href", "") for node in opf.iter()
                            if local_name(node.tag) == "item" and node.attrib.get("id") and "xhtml" in node.attrib.get("media-type", "")}
                available_files = set(archive.namelist())
                spine_paths = [os.path.normpath(os.path.join(base, manifest.get(node.attrib.get("idref", ""), ""))).replace("\\", "/")
                               for node in opf.iter() if local_name(node.tag) == "itemref"]
                spine_paths = tuple(chapter_path if chapter_path in available_files else "" for chapter_path in spine_paths)
                epub_spines[source_key] = spine_paths
            if chapter_index < 0 or chapter_index >= len(spine_paths):
                return "", len(spine_paths)
            chapter_path = spine_paths[chapter_index]
            if not chapter_path:
                return "", len(spine_paths)
            doc = ElementTree.fromstring(archive.read(chapter_path))
            body = next((node for node in doc.iter() if local_name(node.tag) == "body"), doc)
            def render(node):
                tag = local_name(node.tag).lower()
                if tag in ("script", "style", "head", "nav", "svg", "metadata"):
                    return ""
                inner = html.escape(node.text or "")
                inner += "".join(render(child) + html.escape(child.tail or "") for child in node)
                if tag in ("p", "div", "section", "blockquote", "li", "h1", "h2", "h3", "h4", "em", "i", "strong", "b", "sup", "sub"):
                    return f"<{tag}>{inner}</{tag}>"
                return "<br>" if tag == "br" else inner
            rendered = render(body)
            remember_epub_chapter(source_key, chapter_index, rendered, len(spine_paths))
            return rendered, len(spine_paths)

    @staticmethod
    def epub_reader_html(title, chapter, token, item_id, count):
        safe_title = html.escape(title)
        content = f'<section class="epub-chapter" data-chapter="0"><h1>{safe_title}</h1>{chapter}</section>'
        document = Handler.paged_html(title, content)
        document = document.replace(f'<h1>{safe_title}</h1>'.encode("utf-8"), b"", 1)
        script = f'''<script>
        window.chapterLoadingEnabled=true;window.allChaptersLoaded={str(count <= 1).lower()};
        window.virtualBasePage=0;
        const chapterEndpoint='/api/epub-chapter?token='+encodeURIComponent({json.dumps(token)})+'&id='+encodeURIComponent({json.dumps(item_id)});
        let chapterIndex=1,chapterTotal={int(count)},chapterLoading=false,restoringReaderPage=false;
        const chapterRanges=[{{index:0,node:pages.querySelector('[data-chapter="0"]'),start:1,end:Math.max(1,Math.ceil(pages.scrollWidth/(pages.clientWidth||1)))}}];
        function pageCount(){{return Math.max(1,Math.ceil(pages.scrollWidth/(pages.clientWidth||1)))}}
        function currentPage(){{const width=pages.clientWidth||1;return (Number(window.virtualBasePage)||0)+Math.min(pageCount(),Math.floor((pages.scrollLeft+width*.35)/width)+1)}}
        function activeChapterIndex(){{const page=currentPage();let active=chapterRanges[0]?.index||0;for(const range of chapterRanges){{if(page>=range.start)active=range.index;else break}}return active}}
        async function loadChapter(index){{if(chapterLoading)return false;chapterLoading=true;try{{const response=await fetch(chapterEndpoint+'&chapter='+index,{{cache:'no-store'}});const result=await response.json();if(!response.ok)throw new Error(result.error||'章节加载失败');const section=document.createElement('section');section.className='epub-chapter';section.dataset.chapter=String(index);section.innerHTML=result.html||'';pages.append(section);const before=pageCount();chapterRanges.push({{index,node:section,start:(Number(window.virtualBasePage)||0)+before+1,end:(Number(window.virtualBasePage)||0)+pageCount()}});chapterIndex=index+1;window.allChaptersLoaded=chapterIndex>=chapterTotal;return true}}catch(error){{parent.postMessage({{type:'reader-error',message:error.message||'章节加载失败'}},location.origin);return false}}finally{{chapterLoading=false}}}}
        async function loadNextChapter(){{if(window.allChaptersLoaded)return false;const loaded=await loadChapter(chapterIndex);if(loaded)requestAnimationFrame(()=>{{updateProgress();if(!restoringReaderPage){{maybeLoadChapter();pruneChapters()}}}});return loaded}}
        function pruneChapters(){{if(restoringReaderPage||chapterRanges.length<5)return;const cutoff=activeChapterIndex()-2,remove=chapterRanges.filter(range=>range.index<cutoff);if(!remove.length)return;const width=pages.clientWidth||1,oldCount=pageCount(),oldLeft=pages.scrollLeft;for(const range of remove){{range.node.remove()}}chapterRanges.splice(0,remove.length);const first=chapterRanges[0]?.node;if(first)first.style.breakBefore='auto';const removedPages=Math.max(0,oldCount-pageCount());window.virtualBasePage=(Number(window.virtualBasePage)||0)+removedPages;pages.scrollLeft=Math.max(0,oldLeft-removedPages*width);requestAnimationFrame(updateProgress)}}
        async function resetToStart(){{for(const range of chapterRanges)range.node.remove();chapterRanges.length=0;window.virtualBasePage=0;chapterIndex=0;window.allChaptersLoaded=false;await loadChapter(0);requestAnimationFrame(updateProgress)}}
        async function ensureReaderPage(page){{restoringReaderPage=true;page=Math.max(1,Math.floor(Number(page)||1));if(page<=Number(window.virtualBasePage||0))await resetToStart();while(Number(window.virtualBasePage||0)+pageCount()<page&&!window.allChaptersLoaded){{if(!await loadNextChapter())break}}const width=pages.clientWidth||1;pages.scrollTo({{left:Math.max(0,(page-Number(window.virtualBasePage||0)-1)*width),behavior:'auto'}});restoreReady=true;restoringReaderPage=false;requestAnimationFrame(()=>{{updateProgress();pruneChapters();maybeLoadChapter()}})}}
        function maybeLoadChapter(){{if(!restoringReaderPage&&!window.allChaptersLoaded&&pages.scrollLeft+pages.clientWidth*2>=pages.scrollWidth)loadNextChapter()}}
        pages.addEventListener('scroll',()=>{{maybeLoadChapter();pruneChapters()}},{{passive:true}});window.addEventListener('resize',()=>requestAnimationFrame(()=>{{updateProgress();maybeLoadChapter()}}));requestAnimationFrame(()=>{{updateProgress();maybeLoadChapter()}});
        </script>'''.encode("utf-8")
        return document.replace(b"</body></html>", script + b"</body></html>")

    def send_epub_reader(self, path, item, token, item_id, initial_chapter=None):
        CONTENT_PROCESSING_SLOTS.acquire()
        try:
            chapter, chapter_count = initial_chapter or self.epub_chapter(path, 0)
            if not chapter:
                raise ValueError("这本 EPUB 没有可显示的正文。")
            document = self.epub_reader_html(item.get("title", "电子书"), chapter, token, item_id, chapter_count)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(document)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(document)
        finally:
            CONTENT_PROCESSING_SLOTS.release()

    @staticmethod
    def prefetch_epub(item, cache_key):
        with epub_prefetch_lock:
            if cache_key in epub_prefetches or len(epub_prefetches) >= 2:
                return
            epub_prefetches.add(cache_key)
        def download():
            try:
                Handler.cache_epub_from_remote(item, cache_key)
                remote_key = "remote:" + hashlib.sha256(item["download"].encode()).hexdigest()
                epub_spines.pop(remote_key, None)
                clear_epub_chapter_cache(remote_key)
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, zipfile.BadZipFile):
                pass
            finally:
                with epub_prefetch_lock:
                    epub_prefetches.discard(cache_key)
        threading.Thread(target=download, name="epub-prefetch", daemon=True).start()

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BODY:
            raise ValueError("请求内容无效或过大。")
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError("请求内容必须是 JSON 对象。")
        return data

    def handle_shelf_post(self):
        try:
            data = self.read_json()
            token = str(data.get("token", ""))
            item_id = str(data.get("id", ""))
            action = str(data.get("action", ""))
            root = get_opds_session((token, "root"))
            if not root or root.get("expires", 0) < time.time():
                self.send_json(403, {"error": "书库会话已过期，请重新同步书库。"})
                return
            item = get_opds_session((token, item_id))
            if action == "add":
                if not item or not item.get("download") or not item.get("shelfKey"):
                    self.send_json(404, {"error": "找不到这本书，请刷新书库后重试。"})
                    return
                with shelf_lock:
                    records = load_bookshelf()
                    key = item["shelfKey"]
                    accepted_library_ids = library_ids(root["feed"])
                    records = [record for record in records if not (record.get("key") == key and record.get("library") in accepted_library_ids)]
                    records.append({
                        "key": key,
                        "library": library_identity(root["feed"]),
                        "title": item["title"],
                        "author": item["author"],
                        "description": item.get("description", ""),
                        "format": item.get("format", ""),
                        "download": item["download"],
                        "cover": item.get("cover", ""),
                    })
                    save_bookshelf(records)
            elif action == "remove":
                with shelf_lock:
                    records = load_bookshelf()
                    stable_key = item.get("shelfKey", item_id) if item else item_id
                    accepted_library_ids = library_ids(root["feed"])
                    records = [record for record in records if not (record.get("key") == stable_key and record.get("library") in accepted_library_ids)]
                    save_bookshelf(records)
            else:
                self.send_json(400, {"error": "书架操作无效。"})
                return
            self.send_json(200, {"ok": True})
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error) or "书架请求格式无效。"})
        except OSError:
            self.send_json(500, {"error": "无法写入 config/bookshelf.json，请检查宿主机 config 目录权限。"})
        except Exception:
            self.send_json(500, {"error": "更新书架时发生错误。"})

    def handle_shelf_get(self, token):
        root = get_opds_session((token, "root"))
        if not root or root.get("expires", 0) < time.time():
            self.send_json(403, {"error": "书库会话已过期，请重新同步书库。"})
            return
        accepted_library_ids = library_ids(root["feed"])
        try:
            result = []
            with shelf_lock:
                records = load_bookshelf()
            for record in records:
                if not isinstance(record, dict) or record.get("library") not in accepted_library_ids:
                    continue
                item_id = record["key"]
                if origin_of(record.get("download", "")) != origin_of(root["feed"]):
                    continue
                put_opds_session((token, item_id), {
                    "username": root.get("username", ""),
                    "password": root.get("password", ""),
                    "address": root["feed"],
                    "download": record["download"],
                    "cover": record.get("cover", ""),
                    "title": record.get("title", "未命名书籍"),
                    "author": record.get("author", "未知作者"),
                    "description": record.get("description", ""),
                    "shelfKey": item_id,
                    "format": record.get("format", ""),
                    "expires": root["expires"],
                })
                query = f"token={token}&id={item_id}"
                result.append({
                    "id": item_id,
                    "key": item_id,
                    "shelfKey": item_id,
                    "title": record.get("title", "未命名书籍"),
                    "author": record.get("author", "未知作者"),
                    "description": record.get("description", ""),
                    "tag": (record.get("format") or "书架").upper(),
                    "category": "shelf",
                    "kind": "book",
                    "format": record.get("format", ""),
                    "href": f"/api/read?{query}",
                    "coverUrl": f"/api/cover?{query}" if record.get("cover") else "",
                })
            self.send_json(200, {"books": result, "count": len(result)})
        except (OSError, ValueError, json.JSONDecodeError):
            self.send_json(500, {"error": "无法读取 config/bookshelf.json。"})

    def handle_history_get(self, token, view="read"):
        root = get_opds_session((token, "root"))
        if not root or root.get("expires", 0) < time.time():
            self.send_json(403, {"error": "书库会话已过期，请重新同步书库。"})
            return
        try:
            accepted_library_ids = library_ids(root["feed"])
            time_field = "lastViewed" if view == "browse" else "lastRead"
            result = []
            for record in load_reading_history():
                if record["library"] not in accepted_library_ids or not record.get(time_field):
                    continue
                try:
                    if origin_of(record["download"]) != origin_of(root["feed"]):
                        continue
                    cover = record.get("cover", "")
                    if cover and origin_of(cover) != origin_of(root["feed"]):
                        cover = ""
                except ValueError:
                    continue
                item_id = record["key"]
                put_opds_session((token, item_id), {
                    "username": root.get("username", ""), "password": root.get("password", ""),
                    "address": root["feed"], "download": record["download"], "cover": cover,
                    "title": record.get("title", "未命名书籍"), "author": record.get("author", "未知作者"),
                    "description": record.get("description", ""), "shelfKey": item_id,
                    "format": record.get("format", ""), "expires": root["expires"],
                })
                query = f"token={token}&id={item_id}"
                result.append({
                    **record, "id": item_id, "shelfKey": item_id, "kind": "book", "category": "history",
                    "href": f"/api/read?{query}",
                    "coverUrl": f"/api/cover?{query}" if cover else "",
                })
            result.sort(key=lambda item: item.get(time_field, 0), reverse=True)
            self.send_json(200, {"books": result[:100], "count": min(len(result), 100)})
        except (OSError, ValueError, json.JSONDecodeError):
            self.send_json(500, {"error": "无法读取 config/history.json。"})

    def handle_history_post(self):
        try:
            data = self.read_json()
            token, item_id = str(data.get("token", "")), str(data.get("id", ""))
            action = str(data.get("action", "read"))
            if action not in ("read", "browse"):
                self.send_json(400, {"error": "历史记录类型无效。"})
                return
            root = get_opds_session((token, "root"))
            item = get_opds_session((token, item_id))
            if not root or root.get("expires", 0) < time.time() or not item or not item.get("download"):
                self.send_json(403, {"error": "阅读会话已过期，请重新连接书库。"})
                return
            key = item.get("shelfKey") or shelf_key(item["download"], item.get("title", ""), item.get("author", ""))
            now = int(time.time())
            record = {
                "key": key, "library": library_identity(root["feed"]), "title": item.get("title", "未命名书籍"),
                "author": item.get("author", "未知作者"), "description": item.get("description", ""),
                "format": item.get("format", ""), "download": item["download"],
                "cover": item.get("cover", ""),
            }
            with history_lock:
                records = load_reading_history()
                previous = next((existing for existing in records
                                 if existing["key"] == key and existing["library"] == record["library"]), {})
                record["lastRead"] = max(0, int(previous.get("lastRead", 0)))
                record["lastViewed"] = max(0, int(previous.get("lastViewed", 0)))
                record["lastViewed"] = now
                if action == "read":
                    record["lastRead"] = now
                records = [existing for existing in records if not (existing["key"] == key and existing["library"] == record["library"])]
                records.append(record)
                records.sort(key=lambda existing: existing.get("lastRead", 0), reverse=True)
                save_reading_history(records[:500])
            self.send_json(200, {"ok": True})
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error) or "阅读历史请求格式无效。"})
        except OSError:
            self.send_json(500, {"error": "无法写入 config/history.json，请检查 config 目录权限。"})

    def handle_recommendations(self, token):
        now = time.time()
        for cached_token, (expires, _) in list(recommendation_cache.items()):
            if expires <= now:
                recommendation_cache.pop(cached_token, None)
        root = get_opds_session((token, "root"))
        if not root or root.get("expires", 0) < time.time():
            self.send_json(403, {"error": "书库会话已过期，请重新同步书库。"})
            return
        cached = recommendation_cache.get(token)
        if cached and cached[0] > time.time():
            self.send_json(200, {"books": cached[1], "count": len(cached[1])})
            return
        queue, queued, visited, books = [], set(), set(), {}
        sequence = 0
        def enqueue(address, depth, priority=1):
            nonlocal sequence
            if depth > 6 or not address or address in queued or address in visited:
                return
            if origin_of(address) != origin_of(root["feed"]):
                return
            queued.add(address)
            sequence += 1
            heapq.heappush(queue, (priority, depth, sequence, address))
        try:
            enqueue(root["feed"], 0, 0)
            deadline = time.monotonic() + 8
            while queue and len(visited) < 8 and len(books) < 100 and time.monotonic() < deadline:
                _, depth, _, address = heapq.heappop(queue)
                if address in visited:
                    continue
                visited.add(address)
                try:
                    with fetch(address, root.get("username", ""), root.get("password", ""),
                               "application/atom+xml, application/xml, text/xml", timeout=max(0.2, min(2.5, deadline - time.monotonic()))) as response:
                        payload = response.read(4 * 1024 * 1024 + 1)
                    if len(payload) > 4 * 1024 * 1024:
                        continue
                    feed_root = ElementTree.fromstring(payload)
                    result = self.parse_feed(payload, address, root.get("username", ""), root.get("password", ""), token)
                except (HTTPError, URLError, TimeoutError, ValueError, ElementTree.ParseError):
                    continue
                for book in result["books"]:
                    if book["kind"] == "book" and book.get("href"):
                        books[book["shelfKey"] or book["id"]] = book
                    elif book["kind"] == "navigation" and depth < 6:
                        target = get_opds_session((token, book["id"]), {}).get("feed", "")
                        title = book["title"].strip().lower()
                        target_path = urlparse(target).path.lower()
                        all_priority = title in ("all", "全部", "全部书籍", "所有书籍")
                        category_priority = any(term in title or term in target_path
                                                for term in ("分类", "category", "categories", "catalog"))
                        enqueue(target, depth + 1, 0 if all_priority else 1 if category_priority else 2)
                for link in feed_root.iter():
                    if local_name(link.tag) == "link" and "next" in link.attrib.get("rel", "").lower() and link.attrib.get("href"):
                        try:
                            enqueue(same_origin_url(address, link.attrib["href"]), depth, 1)
                        except ValueError:
                            pass
            candidates = list(books.values())[:100]
            recommendation_cache[token] = (min(root["expires"], time.time() + 3600), candidates)
            self.send_json(200, {"books": candidates, "count": len(candidates)})
        except (ValueError, OSError):
            self.send_json(502, {"error": "无法从 Calibre-Web 目录中获取可推荐书目。"})

    def do_POST(self):
        if not self.same_site_post():
            return self.send_json(403, {"error": "已拒绝跨站请求。"})
        if self.path == "/api/login":
            return self.handle_login()
        if self.path == "/api/logout":
            token = self.auth_cookie_token()
            auth_sessions.pop(token, None)
            cookie = "pages_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0" + ("; Secure" if COOKIE_SECURE else "")
            return self.send_json(200, {"ok": True}, {"Set-Cookie": cookie})
        if not self.app_session():
            return
        if self.path == "/api/shelf":
            return self.handle_shelf_post()
        if self.path == "/api/history":
            return self.handle_history_post()
        if self.path != "/api/opds":
            return self.send_json(404, {"error": "接口不存在。"})
        try:
            data = self.read_json()
            address = str(data.get("address", "")).strip()
            username = str(data.get("username", ""))
            password = str(data.get("password", ""))
            with fetch(address, username, password, "application/atom+xml, application/xml, text/xml") as response:
                payload = response.read(8 * 1024 * 1024 + 1)
            if len(payload) > 8 * 1024 * 1024:
                raise ValueError("OPDS 目录超过 8 MB，请使用分页目录地址。")
            token = secrets.token_urlsafe(32)
            put_opds_session((token, "root"), {"username": username, "password": password, "feed": address, "expires": time.time() + 12 * 60 * 60})
            result = self.parse_feed(payload, address, username, password, token)
            if data.get("persist", True):
                save_connection_config(address, username, password)
            else:
                delete_connection_config()
            self.send_json(200, {**result, "token": token, "address": address})
        except HTTPError as error:
            self.send_json(error.code, {"error": "Calibre-Web 返回 HTTP " + str(error.code) + "，请检查账号和地址。"})
        except (URLError, TimeoutError) as error:
            self.send_json(502, {"error": "无法连接书库。请检查地址、网络和服务器状态。"})
        except (ValueError, ElementTree.ParseError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error) or "OPDS 目录格式不正确。"})
        except OSError:
            self.send_json(500, {"error": "无法保存 config/calibre-web.json，请检查宿主机 config 目录权限。"})
        except Exception:
            self.send_json(500, {"error": "读取书库时发生错误。"})

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/") and parsed.path != "/api/auth/status" and not self.app_session():
            return
        if parsed.path == "/api/restore":
            try:
                config = load_connection_config()
                if not config:
                    self.send_json(404, {"error": "尚未保存书库连接。"})
                    return
                address, username, password = config["address"], config.get("username", ""), config.get("password", "")
                with fetch(address, username, password, "application/atom+xml, application/xml, text/xml") as response:
                    payload = response.read(8 * 1024 * 1024 + 1)
                if len(payload) > 8 * 1024 * 1024:
                    raise ValueError("OPDS 目录超过 8 MB，请使用分页目录地址。")
                token = secrets.token_urlsafe(32)
                put_opds_session((token, "root"), {"username": username, "password": password, "feed": address, "expires": time.time() + 12 * 60 * 60})
                result = self.parse_feed(payload, address, username, password, token)
                self.send_json(200, {**result, "token": token, "address": address})
            except HTTPError as error:
                self.send_json(error.code, {"error": "已保存的 Calibre-Web 登录失效（HTTP " + str(error.code) + "），请重新连接。"})
            except (URLError, TimeoutError):
                self.send_json(502, {"error": "无法连接已保存的书库，请检查 NAS 网络或连接设置。"})
            except (ValueError, KeyError, ElementTree.ParseError, json.JSONDecodeError) as error:
                self.send_json(400, {"error": str(error) or "已保存的书库配置无法读取。"})
            return
        if parsed.path == "/health":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/auth/status":
            token = self.auth_cookie_token()
            authenticated = not APP_PASSWORD or auth_sessions.get(token, 0) > time.time()
            self.send_json(200, {"required": bool(APP_PASSWORD), "authenticated": authenticated})
            return
        if parsed.path == "/api/shelf":
            token = parse_qs(parsed.query).get("token", [""])[0]
            return self.handle_shelf_get(token)
        if parsed.path == "/api/history":
            token = parse_qs(parsed.query).get("token", [""])[0]
            view = parse_qs(parsed.query).get("type", ["read"])[0]
            return self.handle_history_get(token, "browse" if view == "browse" else "read")
        if parsed.path == "/api/recommendations":
            token = parse_qs(parsed.query).get("token", [""])[0]
            return self.handle_recommendations(token)
        if parsed.path in ("/api/opds", "/api/book", "/api/read", "/api/cover", "/api/epub-chapter"):
            query = parse_qs(parsed.query)
            token, item_id = query.get("token", [""])[0], query.get("id", [""])[0]
            item = get_opds_session((token, item_id))
            if not item or item["expires"] < time.time():
                self.send_error(403, "Book link expired. Reconnect your library.")
                return
            if parsed.path == "/api/opds":
                try:
                    result = self.load_directory(item["feed"], item["username"], item["password"], token)
                    self.send_json(200, result)
                except HTTPError as error:
                    self.send_json(error.code, {"error": "读取分类失败，HTTP " + str(error.code)})
                except (URLError, TimeoutError):
                    self.send_json(502, {"error": "无法读取分类，请检查 Calibre-Web 连接。"})
                except (ValueError, ElementTree.ParseError) as error:
                    self.send_json(400, {"error": str(error) or "OPDS 分类目录无法解析。"})
                return
            if parsed.path == "/api/epub-chapter":
                try:
                    if item.get("format") != "epub":
                        raise ValueError("这不是 EPUB 书籍。")
                    chapter_index = int(query.get("chapter", ["0"])[0])
                    if chapter_index < 0 or chapter_index > 10000:
                        raise ValueError("章节序号无效。")
                    with epub_cache_lock:
                        cached = epub_cache.get((token, item_id))
                        if cached and (cached[1] < time.time() or not cached[0].is_file()):
                            cached[0].unlink(missing_ok=True)
                            epub_spines.pop(str(cached[0]), None)
                            clear_epub_chapter_cache(cached[0])
                            epub_cache.pop((token, item_id), None)
                            cached = None
                        if cached:
                            epub_cache[(token, item_id)] = (cached[0], time.time() + 6 * 60 * 60)
                            epub_path = cached[0]
                            os.utime(epub_path, None)
                        else:
                            epub_path = None
                    if epub_path:
                        chapter, count = self.epub_chapter(epub_path, chapter_index)
                    else:
                        try:
                            chapter, count = self.epub_chapter(None, chapter_index, item)
                        except RemoteRangeUnsupported:
                            epub_path = self.cache_epub_from_remote(item, (token, item_id))
                            chapter, count = self.epub_chapter(epub_path, chapter_index)
                    self.send_json(200, {"html": chapter, "chapter": chapter_index, "count": count})
                except (OSError, ValueError, KeyError, zipfile.BadZipFile, ElementTree.ParseError, StopIteration) as error:
                    self.send_json(400, {"error": str(error) or "无法读取 EPUB 章节。"})
                return
            if parsed.path == "/api/read":
                response_started = False
                try:
                    book_format = item.get("format", "")
                    if book_format == "pdf":
                        request_headers = {}
                        range_header = self.headers.get("Range", "")
                        if range_header:
                            if not re.fullmatch(r"bytes=\d*-\d*", range_header.strip()):
                                self.send_error(416, "Only one PDF byte range is supported")
                                return
                            request_headers["Range"] = range_header.strip()
                        if self.headers.get("If-Range"):
                            request_headers["If-Range"] = self.headers["If-Range"]
                        with fetch(item["download"], item["username"], item["password"], extra_headers=request_headers) as response:
                            status = getattr(response, "status", 200)
                            self.send_response(status)
                            self.send_header("Content-Type", "application/pdf")
                            self.send_header("Content-Disposition", "inline")
                            self.send_header("Accept-Ranges", response.headers.get("Accept-Ranges", "bytes"))
                            for header in ("Content-Length", "Content-Range", "ETag", "Last-Modified"):
                                value = response.headers.get(header)
                                if value:
                                    self.send_header(header, value)
                            self.send_header("X-Content-Type-Options", "nosniff")
                            self.send_header("Cache-Control", "private, no-store")
                            self.end_headers()
                            while chunk := response.read(64 * 1024):
                                self.wfile.write(chunk)
                        return
                    if book_format == "epub":
                        with epub_cache_lock:
                            cached = epub_cache.get((token, item_id))
                            if cached and cached[1] > time.time() and cached[0].is_file():
                                epub_cache[(token, item_id)] = (cached[0], time.time() + 6 * 60 * 60)
                                epub_path = cached[0]
                                os.utime(epub_path, None)
                            else:
                                epub_path = None
                        if epub_path:
                            self.send_epub_reader(epub_path, item, token, item_id)
                            return
                        cache_key = (token, item_id)
                        try:
                            CONTENT_PROCESSING_SLOTS.acquire()
                            try:
                                first_chapter = self.epub_chapter(None, 0, item)
                            finally:
                                CONTENT_PROCESSING_SLOTS.release()
                            if not first_chapter[0]:
                                raise ValueError("这本 EPUB 没有可显示的正文。")
                            self.prefetch_epub(item, cache_key)
                            self.send_epub_reader(None, item, token, item_id, first_chapter)
                        except RemoteRangeUnsupported:
                            epub_path = self.cache_epub_from_remote(item, cache_key)
                            self.send_epub_reader(epub_path, item, token, item_id)
                        return
                    with fetch(item["download"], item["username"], item["password"]) as response:
                        length = response.headers.get("Content-Length")
                        if length and int(length) > MAX_BOOK_BYTES:
                            raise ValueError("电子书文件超过 120 MB。")
                        payload = response.read(MAX_BOOK_BYTES + 1)
                    if len(payload) > MAX_BOOK_BYTES:
                        raise ValueError("电子书文件超过 120 MB。")
                    if payload.startswith(b"%PDF-"):
                        self.send_response(200)
                        self.send_header("Content-Type", "application/pdf")
                        self.send_header("Content-Length", str(len(payload)))
                        self.send_header("Content-Disposition", "inline")
                        self.send_header("X-Content-Type-Options", "nosniff")
                        self.send_header("Cache-Control", "private, no-store")
                        self.end_headers()
                        self.wfile.write(payload)
                        return
                    CONTENT_PROCESSING_SLOTS.acquire()
                    try:
                        if book_format == "txt":
                            document = self.text_html(payload, item.get("title", "电子书"))
                    finally:
                        CONTENT_PROCESSING_SLOTS.release()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(document)))
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(document)
                except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError, zipfile.BadZipFile, ElementTree.ParseError):
                    if response_started:
                        return
                    fmt = item.get("format", "电子书").upper()
                    body = ("<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><body style='font:16px sans-serif;padding:2em;color:#555;background:#f7f5ef'>无法打开这本书，请确认文件是有效的 " + html.escape(fmt) + " 文件。</body>").encode("utf-8")
                    self.send_response(502)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                return
            target = item["download"] if parsed.path == "/api/book" else item["cover"]
            if not target:
                self.send_error(404, "Media not found")
                return
            try:
                with fetch(target, item["username"], item["password"]) as response:
                    content_type = response.headers.get("Content-Type", "application/octet-stream")
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Cache-Control", "private, max-age=3600")
                    self.end_headers()
                    while chunk := response.read(64 * 1024):
                        self.wfile.write(chunk)
            except HTTPError as error:
                self.send_error(error.code, "Could not load book from Calibre-Web")
            except (URLError, TimeoutError):
                self.send_error(502, "Could not reach Calibre-Web")
            return

        relative = "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
        target = (ROOT / relative).resolve()
        if not target.is_relative_to(ROOT) or not target.is_file():
            self.send_error(404)
            return
        data = target.read_bytes()
        compressible = target.suffix.lower() in (".html", ".js", ".json", ".webmanifest", ".svg", ".css", ".txt")
        compressed = compressible and len(data) >= 1024 and "gzip" in self.headers.get("Accept-Encoding", "").lower()
        if compressed:
            data = gzip.compress(data, compresslevel=5)
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix in (".js", ".json", ".webmanifest"):
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if compressed:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    class LimitedThreadingHTTPServer(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 32

        def __init__(self, address, handler):
            super().__init__(address, handler)
            self.request_slots = threading.BoundedSemaphore(MAX_WORKERS)

        def process_request(self, request, client_address):
            if not self.request_slots.acquire(blocking=False):
                try:
                    request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
                finally:
                    self.shutdown_request(request)
                return
            try:
                super().process_request(request, client_address)
            except Exception:
                self.request_slots.release()
                raise

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                self.request_slots.release()

    print(f"页间阅读器 listening on {HOST}:{PORT} (max workers: {MAX_WORKERS})")
    LimitedThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
