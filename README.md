# PUAA

面向北航学生的多用户日程与提醒系统。Python 单体服务、SQLite 持久化，不包含演示数据。

当前调试范围（2026-10-08）：只开放 **SPOC 作业、课程提醒（教务课表）、JUDGE 作业**。课程签到（iClass）及其他业务显示 `Coming Soon`，不能启用、同步、新建规则或发送提醒；其代码、已有数据和规则配置仍保留。账号、学校登录、通知渠道及用于查看这三类数据的今日/日历/待办页面继续可用。下方接口能力清单记录已保留的实现，不代表全部已开放。

课程提醒只使用 BYXT 教务课表，展示课程名、起止时间、教师和教室，用户可配置上课前或课程结束前多少分钟及通知方式。手动同步、同步全部、自动刷新与规则执行均不调用任何 iClass 接口；旧签到规则停用，不自动转换成课程提醒。课表同步不读取考试接口，也不清除保留的考试和签到历史。

连接页支持单项手动同步和实时进度，来源互不阻塞。待办支持提交状态、来源、作业类型和过期筛选，日期包含年份；今日与日历可切换已完成事项。提醒规则以“连接 → 规则类型”选择：课程提醒/变更（含新增），SPOC 截止/新作业/新评分，JUDGE 截止/新作业。截止提醒支持多个“天 + 小时 + 分钟”提前量，总量至少 1 分钟，可分别筛选提交状态。首次完整同步仅建立变更基线，不发送历史变化。

“账户与安全”中的学期过滤默认开启：2 月 1 日至 9 月 1 日、8 月 1 日至次年 3 月 1 日（结束日期不含当天），重叠期间取有效窗口并集。可以自定义日期或关闭过滤，统一作用于今日、日历、待办和提醒；旧数据不删除，范围外不提醒，JUDGE 已知旧作业不重复读取详情。

今日仅看当天待处理与 7 天内截止事项；日历只提供日/周/月时间安排；待办管理提交、完成和忽略状态。单项忽略不会改变学校的提交记录，同步后保持忽略，不展示在今日/日历且不提醒；可在待办“已忽略”中取消忽略。

周日程完整显示 0–24 点，日期与截止/全天栏固定，时间轴独立滚动；首次定位 8 点，可快捷跳到 0/8/18 点，刷新保留滚动位置。作业按截止时间显示在每日顶部，不虚构持续时长；有起止时间的事项按实际时长排列，跨日拆分、重叠并排，点击查看完整信息。窄屏横向滚动时保留时间刻度。

## 本地一键启动

Windows：

```powershell
.\start.ps1
```

macOS / Linux：

```bash
chmod +x start.sh
./start.sh
```

打开 <http://localhost:8000>。首次启动会创建虚拟环境、安装依赖并复制 `.env.example`。

Windows 脚本会依次探测 `python`、`python3`、`py -3`。Conda 环境无法向 `venv` 写入 pip 时，会优先复用当前环境中已安装且可导入的依赖；缺少依赖时再安装到项目隔离目录。

## Docker Compose

```bash
docker compose up -d --build
```

打开 <http://localhost:11451>。默认仅绑定本机，数据与自动生成的随机密钥保存在 Docker 命名卷，不读取开发机的 `./data`。首次部署无需创建配置文件。

需要修改端口或配置时，复制 `.env.compose.example` 为 `.env.compose`，使用 `docker compose --env-file .env.compose up -d --build`。主机端口通过 `PUAA_COMPOSE_PORT` 配置，与本地 Python 的 `PUAA_PORT` 分离。公网部署需要 HTTPS 与 `PUAA_SECURE_COOKIES=true`。

详细操作、更新与数据保管见 [部署说明](docs/DEPLOYMENT.md)。本轮未在本地构建或运行 Docker。

## 发布与隐私

公开源码不包含个人账户、学校数据、登录会话、通知配置、运行日志、密钥或数据库备份。`.gitignore` 排除整个 `data/` 与本机配置，Docker 构建上下文仅允许应用源码、前端与依赖清单。测试仅使用隔离数据库和模拟响应；设计稿不注入正式程序。

发布前运行 `python scripts/privacy_audit.py --repository .`，检查暂存文件和全部 Git 历史（只报告文件名和风险类型，不输出秘密值）。此检查是发布护栏，不替代人工审查。

## 账户边界

- 用户先注册并登录 PUAA；
- 每个用户再单独连接自己的北航统一认证账号；
- 业务查询全部按 `user_id` 隔离；
- PUAA 密码使用 Argon2id；
- 学校密码默认不保存，用户选择自动同步后才使用 AES-256-GCM 加密保存；
- 正式运行路径没有演示适配器或演示数据开关。

## 已接入学校 API

当前只读连接器：

- 本科教务：
- `GET .../api/home/currentUser.do`（会话探活）
- `GET .../student/schoolCalendars.do`
- `GET .../getTermWeeks.do`
- `POST .../student/getMyScheduleDetail.do`
- `GET .../student/exams.do`
- iClass：当天与次日签到状态；`STATUS=2` 的空响应按当天无可用签到课程处理；
- 成绩应用：默认每 15 分钟检查当前学期成绩，首次建立基线，之后在新成绩出现或分数变化时提醒；
- SPOC：作业截止、提交状态和得分，使用 `mrxq` 学期查询代码；
- JUDGE：作业截止、提交状态和得分；开放时间只保留在详情，日历以截止时间归档；
- 博雅：已选课程、签到/签退窗口与状态；
- 阳光体育：使用体育系统自己的学期截止时间；只有剩余次数达到“本周剩余额度 + 后续每周上限”时才进入达标风险提醒；
- 体育场馆：个人预约时间、场地与审批状态；
- 图书馆：个人座位预约时间、地点、座位号与状态。

运行时不依赖 UBAA。UBAA、buaa-api、duaa 只用于交叉核对学校协议，不作为上游服务。每个连接器公开版本、能力、端点与参考源码清单；单一路由失败会单独显示且不会清空旧数据。当前不会代替用户打卡、签到、预约、取消预约或提交作业。

勾选“加密保存”后，后台会自动重建学校会话。每个数据源都可在“连接”页独立设置为手动、5/15/30 分钟、1/6 小时或每天刷新；后台每分钟检查一次到期的数据源。

提醒规则按业务类型显示不同设置：课程可设置上课前提醒，签到可设置下课前检查未签到，图书馆座位/研讨室和场馆预约可分别设置开始前与离开失效前提醒，作业、考试、成绩、阳光体育、博雅和自定义日程也都有各自字段。邮件、WxPusher、Telegram 和自定义 HTTPS Webhook 可多选投递；Webhook 会发送 `title`、`message`、`to` 三个 JSON 字段，本项目没有内置绑定任何短信厂商。

## 主要 API

- `POST /api/account/register|login|logout`
- `GET /api/session`
- `POST /api/school/preload|login|disconnect`
- `GET /api/school/captcha/{id}`
- `GET /api/dashboard`
- `GET|POST /api/events`
- `POST /api/sources/sync`、`PATCH /api/sources/{id}`
- `GET /api/connectors`
- `GET|POST /api/rules`、`PUT|PATCH|DELETE /api/rules/{id}`
- `GET /api/rule-templates|triggers|deliveries|inbox`
- `GET|POST /api/channels`、`PATCH|DELETE /api/channels/{id}`
- `PUT /api/preferences`

接口文档：<http://localhost:8000/docs>

产品范围与实现顺序见 [SYSTEM_DESIGN.md](SYSTEM_DESIGN.md)、[docs/FEATURE_MATRIX.md](docs/FEATURE_MATRIX.md) 和 [docs/DELIVERY_PLAN.md](docs/DELIVERY_PLAN.md)。
