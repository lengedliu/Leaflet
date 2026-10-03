# 页间移动端阅读器

可直接用 Docker Compose 部署的移动端 PWA，OPDS 请求由同源 Python 服务代理，支持 Calibre-Web Atom 目录、逐层浏览、封面代理、搜索和 EPUB、PDF、TXT 阅读。iOS 原生 SwiftUI 工程另见 `iOS/`。

## Docker 部署

请通过 HTTP 服务访问本应用；直接双击 `index.html`（`file://`）会被浏览器安全策略阻止调用 OPDS 代理、配置和封面接口。开发预览可在项目目录运行 `python server.py`，再打开 `http://127.0.0.1:8080/`。开发服务器默认只监听本机，且不启用应用密码，不要把它直接暴露到局域网或公网。

首次部署时复制 `.env.example` 为 `.env`，将 `APP_PASSWORD` 改成唯一且至少 12 位的密码。然后在服务器目录运行：

```sh
docker compose up -d --build
```

应用默认要求通过 HTTPS 反向代理访问（`COOKIE_SECURE=true`）。配置好 TLS 代理并将外部流量转发至容器 8080 端口后，手机访问代理域名，先输入阅读器密码，再点连接按钮填写 Calibre-Web 的 OPDS URL、用户名和密码。仅可信家庭局域网且明确接受明文传输时，才在 `.env` 中设 `COOKIE_SECURE=false` 并通过防火墙限制端口。可在 Safari 中使用“添加到主屏幕”。

`compose.yaml` 默认将宿主机 `./config` 映射到容器 `/config`。地址和 Calibre-Web 凭据以 JSON 保存在 `config/calibre-web.json`，书架条目保存在 `config/bookshelf.json`，阅读历史保存在 `config/history.json`；容器重启后都会保留。书架和历史只存书目、下载地址及最近阅读时间，阅读时通过当前已连接的书库会话获取原文件。Calibre-Web 凭据文件含明文密码；应用登录密码存于 `.env`，两者都应限制宿主机目录访问。服务会尽量设置配置文件权限为仅所有者可读写。Linux 主机首次部署前可运行 `mkdir -p config && sudo chown 10001:10001 config`，确保容器用户有权写入配置目录。OPDS 地址必须能从 Docker 容器访问。

Docker 的 HTTP 接口需要应用登录；同源 API 有登录会话保护，跨站 POST 会被拒绝，OPDS 链接与重定向限制在书库同一主机。PDF 使用流式代理并支持单段 HTTP Range，降低大文件占用内存；EPUB 限制最大文件和解压后总大小。请不要将 Docker 端口直接暴露到公网。

## 当前范围

- 移动优先响应式界面和可安装 PWA 外壳
- OPDS Atom 目录与书目解析、分类逐层浏览、搜索、登录保护的封面代理
- 可从书籍卡片加入或移出“我的书架”；书架清单写入 Docker 映射目录 `config/bookshelf.json`，重启后保留
- 首页显示最近阅读历史与随机推荐；阅读历史写入 `config/history.json`，按书库隔离并在容器重启后恢复
- 底部“我”页提供个人阅读时长排行、在读/读完列表、喜欢的书单、浏览记录和 OPDS 连接入口；时长与读完标记保存在当前浏览器，喜欢的书使用持久化书架
- 点击书籍先查看详情、简介和封面，再选择开始阅读或加入书架；Calibre-Web 未提供简介时会显示提示
- EPUB、TXT 正文转为适配手机的横向分页阅读页面，支持触控、键盘和桌面左右箭头翻页；PDF 在阅读器中使用浏览器内置 PDF 查看器，并可用桌面箭头切换页码
- EPUB、TXT 支持阅读进度恢复、单书书签、字号调节和护眼/白纸/夜间主题；阅读状态保存在浏览器本地
- Docker 镜像以非 root 用户运行，容器根文件系统只读
- iOS 原生工程可独立在 Xcode 中打开

EPUB 阅读器首版提取正文并以适配手机的分页样式显示；TXT 支持 UTF-8、UTF-16 与 GB18030 解码。EPUB 复杂排版与插图、离线阅读、跨设备阅读位置同步仍需后续开发。
