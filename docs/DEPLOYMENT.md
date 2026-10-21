# Docker Compose 部署

需要 Docker Engine / Docker Desktop 和 Compose v2。不需要本机 Python、Node 或额外数据库。

## 一键启动

```bash
git clone https://github.com/lixu10/BUAA-Pusher.git
cd BUAA-Pusher
docker compose up -d --build
```

打开 <http://localhost:11451>，或使用 `http://服务器公网IP:11451`。默认绑定 `0.0.0.0:11451`，可经已放行的公网网卡访问。新部署不包含任何演示数据或开发者数据；正式连接学校账户前请配置 HTTPS，避免明文传输登录信息。

容器内部监听 8000，主机默认端口为 **11451**。数据库和自动生成的随机凭据密钥保存在 Docker 命名卷 `puaa_data`，不会读取本机 `./data`。

已有本地 `.env` 时，Compose 会读取其中同名变量；主机端口使用独立的 `PUAA_COMPOSE_PORT`，不会被本地 Python 的 `PUAA_PORT=8000` 覆盖。要明确使用部署配置：复制 `.env.compose.example` 为 `.env.compose`，然后运行：

```bash
docker compose --env-file .env.compose up -d --build
```

`.env.compose` 仅供本机使用，不得提交到 Git。主机端口可用 `PUAA_COMPOSE_PORT` 调整，本地 Python 启动默认仍为 8000。

## 状态、更新与停止

### 学校连接的 WebVPN 模式

JUDGE 直连依赖校内网络。所有学校连接可在对应行选择“学校 WebVPN”，按用户、按连接保存，无需修改 Compose 或部署 UBAA。Coming Soon 连接只能预设模式，仍不能启用。

已选择加密保存学校密码的用户，下次单独同步或定时刷新时会自动建立 WebVPN 会话。未保存密码时，点击 JUDGE 行的“登录”，输入同一个学校账号并按需手动填写验证码；此过程不会断开直连会话，也不会清空旧作业。服务器重启后只恢复选择，不持久化登录 Cookie。

模式切换不会删除数据或改变学校状态；超时、验证码或认证失败保留旧数据，同步或签到检查期间不能切换该连接模式。iClass 恢复为默认停用的只读检查：启用后设置自动同步间隔（也可选手动）、单独同步，再添加规则，不提交签到或作业。升级一次性停用旧签到连接，保留同步间隔；后续重启保留用户新的启用与间隔选择。直接连接 iClass 验证 TLS，证书或校外访问失败可选 WebVPN，不自动关闭证书校验。

即使已保存密码，遇到验证码时也会显示“登录”，由用户手动完成验证，不反复自动尝试验证码。

### 维护命令

```bash
docker compose ps
docker compose logs --tail=100 puaa
git pull --ff-only
docker compose up -d --build
docker compose down
```

健康检查读取 `/api/health`；进程退出时自动重启。停止或重建容器保留数据卷。**不要执行 `docker compose down -v`**，它会删除包含数据库和密钥的数据卷。

## 公网访问

默认绑定 `0.0.0.0`，主机端口为 `11451`。云服务器需在安全组和宿主机防火墙放行 TCP 11451；发布容器端口并不会自动修改云安全组。不需要暴露数据库或其他端口。

公网 IP 直连的 HTTP 不加密密码和会话。正式使用学校账号前应通过域名和 HTTPS 反向代理访问，并将 `.env.compose` 中 `PUAA_SECURE_COOKIES=true`。不要在 HTTP 页面启用此选项，否则浏览器不会发送登录 Cookie。

若只允许宿主机反向代理访问，设置 `PUAA_BIND_HOST=127.0.0.1` 后重新创建容器，再由 HTTPS 反向代理转发到 `127.0.0.1:11451`。限制防火墙访问来源，不向互联网公开数据库、配置文件、学校凭据或未认证的管理服务。已有 `.env`／`.env.compose` 显式设置的绑定地址会覆盖新默认值。

## 数据与备份

源码发布不包含 `.env*`、数据库、学校登录会话、账号、密钥、日志或备份；容器构建上下文也使用源码白名单。

数据库与 `.master_key` 必须一并备份；显式使用 `PUAA_MASTER_KEY` 时，另行安全保管该值。遗失或改变密钥后无法解密之前保存的学校凭据和通知配置。备份包含隐私，不要上传到仓库或公共存储。

更换服务器时，应停止写入后备份整个数据卷，并恢复至新服务器的命名卷；不要把运行中的 SQLite 文件直接当作一致性备份。首次公开发布不迁移开发机的个人数据。

## 部署验证范围

本项目提供配置静态检查、隔离后端测试和前端测试。本轮按要求未在本地构建或运行 Docker，目标服务器首次部署时仍应检查 `docker compose ps` 的健康状态与日志。
