# Docker Compose 部署

需要 Docker Engine / Docker Desktop 和 Compose v2。不需要本机 Python、Node 或额外数据库。

## 一键启动

```bash
git clone https://github.com/lixu10/BUAA-Pusher.git
cd BUAA-Pusher
docker compose up -d --build
```

打开 <http://localhost:11451>，注册程序账户，再连接自己的学校账户。新部署不包含任何演示数据或开发者数据。

容器内部监听 8000，主机默认端口为 **11451**。数据库和自动生成的随机凭据密钥保存在 Docker 命名卷 `puaa_data`，不会读取本机 `./data`。

已有本地 `.env` 时，Compose 会读取其中同名变量；主机端口使用独立的 `PUAA_COMPOSE_PORT`，不会被本地 Python 的 `PUAA_PORT=8000` 覆盖。要明确使用部署配置：复制 `.env.compose.example` 为 `.env.compose`，然后运行：

```bash
docker compose --env-file .env.compose up -d --build
```

`.env.compose` 仅供本机使用，不得提交到 Git。主机端口可用 `PUAA_COMPOSE_PORT` 调整，本地 Python 启动默认仍为 8000。

## 状态、更新与停止

### 校外访问 JUDGE

JUDGE 直连依赖校内网络。首次连接学校账户后，在“连接 → JUDGE 作业”选择“学校 WebVPN”。该设置按用户持久化，无需修改 Compose 或部署 UBAA。

已选择加密保存学校密码的用户，下次单独同步或定时刷新时会自动建立 WebVPN 会话。未保存密码时，点击 JUDGE 行的“登录”，输入同一个学校账号并按需手动填写验证码；此过程不会断开直连会话，也不会清空旧作业。服务器重启后只恢复选择，不持久化登录 Cookie。

模式切换不会删除数据或改变学校作业状态。WebVPN 超时、需要验证码或认证失败时保留旧数据；当前同步完成前不能切换模式。仍不调用 iClass、提交作业或其他业务写接口。

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

默认只绑定 `127.0.0.1`，可由宿主机 HTTPS 反向代理访问 `127.0.0.1:11451`。公网部署需要 HTTPS，并将 `.env.compose` 中 `PUAA_SECURE_COOKIES=true`。不要在 HTTP 页面启用此选项，否则浏览器不会发送登录 Cookie。

若需要局域网直连，设置 `PUAA_BIND_HOST=0.0.0.0`，并限制防火墙访问来源。不要直接向互联网开放明文 HTTP、数据库、学校凭据或管理接口。

## 数据与备份

源码发布不包含 `.env*`、数据库、学校登录会话、账号、密钥、日志或备份；容器构建上下文也使用源码白名单。

数据库与 `.master_key` 必须一并备份；显式使用 `PUAA_MASTER_KEY` 时，另行安全保管该值。遗失或改变密钥后无法解密之前保存的学校凭据和通知配置。备份包含隐私，不要上传到仓库或公共存储。

更换服务器时，应停止写入后备份整个数据卷，并恢复至新服务器的命名卷；不要把运行中的 SQLite 文件直接当作一致性备份。首次公开发布不迁移开发机的个人数据。

## 部署验证范围

本项目提供配置静态检查、隔离后端测试和前端测试。本轮按要求未在本地构建或运行 Docker，目标服务器首次部署时仍应检查 `docker compose ps` 的健康状态与日志。
