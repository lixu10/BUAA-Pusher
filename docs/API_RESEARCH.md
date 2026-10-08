# 北航 API 调研记录

## 2026-10-08 规则对比与 SPOC 评分核验

- 连接分别定义二级规则：BYXT 上课提醒/课程变更（含增课和取消）；SPOC 截止/新作业/新评分；JUDGE 截止/新作业。没有统一套用三种类型。
- 作业截止时间为多个独立提前量，每项“天 + 小时 + 分钟”，总量至少 1 分钟；0 天不是截止时发送，0 天 2 小时 30 分钟是提前 150 分钟。删除原“再次提醒”输入与不明数值作业类型筛选。
- 阅读 UBAA `LocalSpocApi.kt` / `LocalSpocSupport.kt`：现有个人提交查询为只读 GET `kczy/queryXsSubmitKczyInfo?kczyid=...`，详情为 GET `kczy/queryKczyInfoByid?id=...`。不接入 UBAA 服务，不增加学校业务写请求。
- 官方页面 `https://spoc.buaa.edu.cn/spocnew/` 的学生详情组件 `js/chunk-8b944e98.5a637dbe.js` 明确将 `zyfs` 展示为“作业总分”、`pf` 为“作业得分”，`pyfs=2` 为教师已打分。`js/chunk-727ad68f.37fd1e10.js` 读取个人提交 `pf`、`py`、`pyfs` 为评分、评语、批阅状态；`pyfs=3` 涉及学生互评，不判定为教师新评分。
- 按实际接口字段保存 `max_score` / `earned_score` / `grade_comment` / `grading_status`，零分有效。不再把 `mf` / `zyfs` 满分当个人成绩。未知状态不猜测；首次评分快照仅建基线。
- 本轮核实了官方静态前端字段与隔离模拟响应，尚未用真实已批阅样本验证所有返回形式。`pysj` 仅在接口实际提供时保存，无批阅时间不虚构。
- 教师批阅后的实际返回形式仍需脱敏样本验收。“首次教师批阅→通知”的变更链由内存数据库、模拟学校响应验证，不在真实账号制造分数或发送测试通知。公开记录不保留个人账号的作业数量或批阅统计。
- 完整同步快照和变更通知独立持久化：首次完整同步静默，失败/部分同步不更新对比基线、不推断取消，重复同步不重复通知；新建/重新启用规则不补发历史变更。变更规则复用现有渠道、安静时段和发送失败退避。
- 课程按原外部 ID 优先匹配；课程/教学周内仅剩一条旧课与一条新课时识别为改时间。多条同时变化且无法唯一匹配时，保守标为新增/取消，不凭空确定对应关系。
- 学期过滤和忽略设置同时约束变更通知；评分提醒不受“已提交”或作业截止已过限制。截止提醒在安静时段延迟和重试后仍必须早于截止。

## 2026-10-08 功能范围变更

- 当前开放来源改为 SPOC、BYXT 课程提醒、JUDGE；iClass 课程签到显示 `Coming Soon`，手动、批量、定时和内部同步入口均拦截该来源，不调用任何 iClass 接口。
- 课程提醒读取 BYXT 的当前学期、教学周及 `getMyScheduleDetail.do`，使用 `CourseReminderAdapter` 只导入课程，不请求考试接口。保留其他种类的历史记录。
- 课程展示“待上课 / 上课中 / 已结束”，不使用签到或作业提交状态；规则可配置上课前分钟数及既有通知方式，旧签到规则不迁移、不发送。

## 2026-10-07 本轮修订

- 当前仅开放 SPOC、iClass、JUDGE。同步请求返回后台任务，不再等待三个来源全部结束；各来源单独同步、显示进度、超时保留旧数据。
- iClass 登录链参考 UBAA `SigninClient`；保留每用户会话复用、一次重新授权和明确的授权失败提示。当前功能停用，不调用任何 iClass 接口。
- SPOC 增加 `kczy/queryKczyInfoByid?id=...` 和 `kczy/queryXsSubmitKczyInfo?kczyid=...`，按 UBAA `LocalSpocApi` 读取详情与个人提交。未知值保持未知，不猜测完成。
- JUDGE 按 UBAA `JudgeSupport` 读取逐题状态，区分未提交、部分提交、全部提交、未知；学校返回 HTML，不存在这里可直接替换成的稳定 JSON 提交状态接口。范围外已有历史记录复用缓存，初次遇到没有日期的任务仍需读取详情才能判断所属范围。
- 学期过滤默认开启：每年 2 月 1 日至 9 月 1 日、8 月 1 日至次年 3 月 1 日，结束日期不含当天。重叠时取当前日期所在有效窗口的并集。用户可覆盖起止日期或关闭过滤。按作业截止时间筛选；缺失时间保留为未知。显示、提醒和 JUDGE 历史详情跳过均使用同一用户配置，不删除历史数据。
- 只采用学校实际返回的作业类型字段；缺失类型保持未分类，筛选不会凭课程或标题猜测类型。
- 学期设置位于“账户与安全”，筛选统一应用于所有展示页面和提醒；单项忽略为独立本地字段，学校同步不覆盖它，取消忽略恢复符合规则的未来计划而不会重发已发送通知。

调研日期：2026-09-20。以下结论来自项目源码、维护文档和本项目只读连接器的真实响应验证；没有对签到、预约等写接口做在线探测。

## UBAA

主要参考：

- [总体架构](https://github.com/BUAASubnet/UBAA/blob/main/docs/tech/architecture.md)
- [服务端路由](https://github.com/BUAASubnet/UBAA/blob/main/docs/tech/server-routes.md)
- [shared API](https://github.com/BUAASubnet/UBAA/blob/main/docs/tech/shared-api.md)
- [鉴权与连接模式](https://github.com/BUAASubnet/UBAA/blob/main/docs/features/auth-and-connection.md)
- [课程与考试](https://github.com/BUAASubnet/UBAA/blob/main/docs/features/schedule-and-exam.md)
- [签到](https://github.com/BUAASubnet/UBAA/blob/main/docs/features/signin.md)
- [JUDGE](https://github.com/BUAASubnet/UBAA/blob/main/docs/features/judge.md)
- [图书馆预约](https://github.com/BUAASubnet/UBAA/blob/main/docs/features/libbook.md)

UBAA 支持本地直连、WebVPN 和服务端中继。本项目只把其源码作为协议参考，运行时直接访问北航系统。

### 学校统一认证

| 方法 | 校方地址 |
|---|---|
| GET / POST | `https://sso.buaa.edu.cn/login` |
| GET | `https://sso.buaa.edu.cn/captcha?captchaId=...` |
| GET | `https://uc.buaa.edu.cn/api/login?target=...` |
| GET | `https://uc.buaa.edu.cn/api/uc/status` |
| GET | `https://sso.buaa.edu.cn/logout` |

登录页先解析隐藏字段和 `execution`；表单提交包含 `username/password/_eventId/type`，按需附带验证码。登录后以用户中心 `code == 0` 且存在 `data` 作为会话有效依据。来源：[CasParser](https://github.com/BUAASubnet/UBAA/blob/main/server/src/main/kotlin/cn/edu/ubaa/auth/upstream/CasParser.kt) 与 [AuthService](https://github.com/BUAASubnet/UBAA/blob/main/server/src/main/kotlin/cn/edu/ubaa/auth/api/AuthService.kt)。

### 课程与考试

| 方法 | 校方地址 |
|---|---|
| GET | `https://byxt.buaa.edu.cn/jwapp/sys/homeapp/api/home/student/schoolCalendars.do` |
| GET | `.../api/home/getTermWeeks.do?termCode=...` |
| POST | `.../api/home/student/getMyScheduleDetail.do` |
| GET | `.../api/home/teachingSchedule/detail.do?rq=...&lxdm=student` |
| GET | `.../api/home/student/exams.do?termCode=...` |

课程 DTO 包含课程编号、名称、学分、起止时间、节次、地点、教师、周次与星期；今日课程是较精简的 `bizName/place/time/shortName`。来源：[Schedule DTO](https://github.com/BUAASubnet/UBAA/blob/main/shared/src/commonMain/kotlin/cn/edu/ubaa/model/dto/Schedule.kt)。

### BYXT 会话建立与失效判定

仅完成统一认证登录并不代表已经获得本科教务会话。当前实现按两份独立开源实现交叉校验：

1. 先携带已有 SSO Cookie 请求 `https://sso.buaa.edu.cn/login`，并把 `service` 设为 `https://byxt.buaa.edu.cn/jwapp/sys/homeapp/index.do?contextPath=/jwapp`，完成 BYXT 专属 CAS ticket 交换。来源：[buaa-api AAS 登录](https://github.com/fontlos/buaa-api/blob/main/src/api/aas/core.rs)。
2. 再请求 `.../api/home/currentUser.do` 探活。401、跳回 SSO、返回统一认证 HTML 均归类为 `login_required`；非 200 或非 JSON 内容归类为上游不可用/协议变化。来源：[UBAA ByxtService](https://github.com/BUAASubnet/UBAA/blob/main/server/src/main/kotlin/cn/edu/ubaa/auth/upstream/ByxtService.kt)。
3. 只有探活成功后才请求学期、周次、课表和考试。考试端点另由 [UBAA ExamService](https://github.com/BUAASubnet/UBAA/blob/main/server/src/main/kotlin/cn/edu/ubaa/exam/ExamService.kt) 复核。

应用内的连接器 manifest 固化版本、能力、端点和参考源码；回归测试会验证“先换票、再探活”的顺序。同步失败不会清空上次成功保存的日程，并在数据源状态中区分需要重新登录和上游协议异常。

### 其他校方系统

- iClass：先从 SSO 跳转解析 `loginName`，再由 `iclass.buaa.edu.cn:8347/app/user/login.action` 换取 `sessionId`，课程读取为 `app/course/get_stu_course_sched.action`；业务域使用与 UBAA 一致的独立兼容客户端。按 UBAA 行为，无错误信息的 `STATUS=2` 空列表表示当天无可用签到课程，不应直接判为会话失效。当前连接停用。来源：[SigninClient](https://github.com/BUAASubnet/UBAA/blob/main/server/src/main/kotlin/cn/edu/ubaa/signin/SigninClient.kt) 与 [SigninLoginNameSupport](https://github.com/BUAASubnet/UBAA/blob/main/shared/src/commonMain/kotlin/cn/edu/ubaa/api/SigninLoginNameSupport.kt)。
- SPOC：从 `spocnewht/cas` 重定向提取 token，再调用 `spocnewht/sys/casLogin` 获取角色；`queryOne` 返回的 `mrxq` 才是作业查询代码，`dqxq` 只是学期显示名称。使用 `dqxq` 可能成功返回空列表，应使用接口实际给出的 `mrxq`。作业列表为 `inco/ht/queryListByPage`，课程名通过 `jxkj/queryKclb` 补全。来源：[LocalSpocApi](https://github.com/BUAASubnet/UBAA/blob/main/shared/src/commonMain/kotlin/cn/edu/ubaa/api/local/LocalSpocApi.kt)。
- JUDGE：CAS service 指向 `judge.buaa.edu.cn`，业务页面为 `courselist.jsp` 与 `assignment/index.jsp`，返回 HTML 而非稳定 JSON。来源：[JudgeClient](https://github.com/BUAASubnet/UBAA/blob/main/server/src/main/kotlin/cn/edu/ubaa/judge/JudgeClient.kt)。
- 图书馆：`booking.lib.buaa.edu.cn/v4/login/cas` 完成 CAS 交换，后续 `/v4/*` 请求使用独立 bearer token。当前 H5 回调把 `cas` 放在 URL fragment 中，`login/user` 的 `member` 也可能返回列表；连接器已兼容这两种真实结构。图书馆域当前证书链在部分 Windows 环境不完整，因此只对该业务域使用兼容 TLS 客户端，SSO 校验仍保持严格。来源：[LibBookClient](https://github.com/BUAASubnet/UBAA/blob/main/server/src/main/kotlin/cn/edu/ubaa/libbook/LibBookClient.kt)。
- 阳光体育：智慧北航 OAuth 入口取得 `code`，再由 `campusAppLogin` 换取业务 `uid/token`；进度读取使用 `Classify/getList`、`Clockin/getCount`、`Term/get`。截止时间直接使用 `Term/get.end_time`，不再用教务周历估算；结合 `week_num/week_count` 计算从本周起的最大可完成次数。来源：[LocalYgdkApi](https://github.com/BUAASubnet/UBAA/blob/main/shared/src/commonMain/kotlin/cn/edu/ubaa/api/local/LocalYgdkApi.kt)。
- 博雅：`sscv/cas/login` 建立业务会话；请求体使用随机 AES-ECB 密钥加密，密钥和 SHA-1 摘要再由 RSA PKCS#1 v1.5 加密后放入 `ak/sk` 头。只读调用 `getAllConfig` 和 `queryChosenCourse`。来源：[BykcClient](https://github.com/BUAASubnet/UBAA/blob/main/server/src/main/kotlin/cn/edu/ubaa/bykc/BykcClient.kt) 与 [BykcCrypto](https://github.com/BUAASubnet/UBAA/blob/main/server/src/main/kotlin/cn/edu/ubaa/bykc/BykcCrypto.kt)。
- 体育场馆：`sso/manageLogin` 写入业务 SSO Cookie，`/api/login` 换取 `cgAuthorization`；每次请求按路径、参数与时间戳生成 MD5 签名。个人预约读取为 `/api/orders/mine`。来源：[CgyyZhjsClient](https://github.com/BUAASubnet/UBAA/blob/main/server/src/main/kotlin/cn/edu/ubaa/cgyy/CgyyZhjsClient.kt) 与 [CgyySigner](https://github.com/BUAASubnet/UBAA/blob/main/server/src/main/kotlin/cn/edu/ubaa/cgyy/CgyySigner.kt)。

## buaa-api

[fontlos/buaa-api](https://github.com/fontlos/buaa-api) 是 Rust 库，`Context` 管理 SSO 登录、自动刷新与认证存储，模块覆盖 aas、app、boya、class、spoc 等。它适合作为协议参考，不适合作为 Python 运行时依赖；仓库也明确提示接口可能过时。

## duaa

[singledog957/duaa](https://github.com/singledog957/duaa) 使用 Rust 实现签到轮询，配置包括学号、密码、课程 ID、轮询周期和自动签到窗口。其运行结构是 poller → scheduler/queue → worker，参考源码：[main.rs](https://github.com/singledog957/duaa/blob/main/src/main.rs)、[pipeline](https://github.com/singledog957/duaa/tree/main/src/pipeline)。

本项目借鉴其“轮询与执行分离”，但默认只生成未签到提醒，不自动签到。

## 当前实现结论

- Python 后端维护校方 Cookie，浏览器不直接跨域请求学校系统。
- 已直连 SSO 与 BYXT 的学期、周次、周课表、考试四个只读接口，并增加 BYXT CAS 换票与 `currentUser.do` 探活。
- 已按 UBAA 当前实现接入 iClass 登录跳转与签到状态、成绩应用、SPOC 二次 token/RoleCode/AES-CBC 参数、JUDGE CAS 与 HTML 页面解析、博雅 RSA/AES 协议与已选课程、阳光体育 OAuth/进度、体育场馆签名与个人预约、图书馆 CAS/个人预约；所有连接器只读。
- 连接器以版本化 manifest 暴露能力、端点和参考源码；协议解析已覆盖自动化测试。公开记录只保留协议结论，不保留个人账号的导入数量、课表、成绩或预约记录。
- 用户选择加密保存学校密码后，后台按每个数据源各自的周期同步；单连接器失败不会阻断其他连接器，也不会删除该来源上次成功的数据。某来源成功完成全量同步后，才清除它已不存在的旧事件。
- 先保留上游原始字段，再逐步针对真实样本收紧 DTO。
- 图书馆预约/取消、阳光打卡、课堂签到等写操作需要单独授权、审计与防重复机制，当前均未调用。

## 面向完整产品的连接器清单

| 连接器 | 目标能力 | 已知校方系统 | 调研状态 |
|---|---|---|---|
| `byxt` | 学期、教学周、课程、考试 | `byxt.buaa.edu.cn` | 课表协议已验证；当前仅开放课程 |
| `iclass` | 课程签到状态 | `iclass.buaa.edu.cn` | 保留实现，当前停用 |
| `grade` | 成绩与出分变化 | `app.buaa.edu.cn/buaascore` | 保留实现，当前停用 |
| `spoc` | 课程与作业 | `spoc.buaa.edu.cn` | 查询协议已验证，使用 `mrxq` |
| `judge` | 课程、作业、提交与得分 | `judge.buaa.edu.cn` | 查询协议已验证；HTML 解析仍需回归保护 |
| `bykc` | 已选课程、签到/签退窗口与状态 | `bykc.buaa.edu.cn` | 只读实现完成，签到状态语义待真实样本验收 |
| `cgyy` | 体育场馆个人预约与审批状态 | `cgyy.buaa.edu.cn` | 只读实现完成，待真实样本验收 |
| `ygdk` | 阳光跑/TD 进度 | `ygdk.buaa.edu.cn` | 保留实现，当前停用 |
| `libbook` | 个人预约；后续区域/座位观察 | `booking.lib.buaa.edu.cn` | 保留实现，当前停用；座位开放观察未实现 |
| `classroom` | 空闲教室/研讨空间 | `app.buaa.edu.cn` 等 | 部分定位，需确认研讨室独立接口 |
| `party` | 党组织活动 | 未确认 | 先提供共享来源/ICS/自定义接入 |

正式开发前，每个连接器要建立脱敏响应 fixture、字段字典、失效判定和速率策略。只有“路由已定位”不能视为功能完成。
