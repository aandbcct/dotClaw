# PA-02 提醒与通知总体设计及实施计划

设计基线为 2026-11-19 的 `feat/assistant`、提交 `98a6b99` 和本次已确认讨论。覆盖 #61、#62、#63，按依赖完成六阶段 commit。实现已交付；按用户要求未运行 pytest、Vitest、应用或浏览器动态验证。已完成 Python 源码编译静态检查、前端 TypeScript/Vite 构建与文档/差异检查。详细契约唯一来源为 [spec.md](spec.md)。

## 1. 目标与范围

用户在事务内设置多条一次性或周期提醒，后端持续调度、持久保存触发和通知，网页打开时可收到浏览器系统通知。停机恢复产生一条汇总通知，事务结束后停止后续提醒。

必做功能包括指定时间、截止前偏移、每日/每周/每月/每年周期，修改/暂停/恢复/取消，事务状态和删除联动，持久触发记录、通知历史和已读状态，恢复摘要、关联事务查看、浏览器通知授权与投递结果，以及旧提醒模块清理。复用 APScheduler 的唤醒与日期计算能力。

非目标包括周期任务实例/打卡、自然语言操作、外部渠道连接器、原生 Windows 常驻程序、Web Push、关闭网页后系统通知、多用户与跨主机调度。渠道接口只承载当前网页投递调用，不增加插件注册、通用消息队列或未来渠道配置。

## 2. 现状与目标差异

| 核对来源 | 已存在的事实 | 本次改造 |
| --- | --- | --- |
| README.md、docs/docker.md | 本地单进程应用；Docker Linux 容器提供 Web；README 的 Scheduler 标为尚未接入 | 将调度接入现有宿主；说明浏览器通知的页面存活与权限要求 |
| personal_tasks/models.py、service.py | 独立事务、四种状态、日期/时刻截止、版本更新和逾期派生计算 | 复用事务语义；新增关联提醒；结束、改期、删除在存储提交中联动 |
| personal_tasks/sqlite_repository.py | 每次调用短连接、to_thread、BEGIN IMMEDIATE、FULL 同步，数据库版本 1 | 原库升级版本 2；原子写提醒、触发、通知和扫描水位 |
| scheduler/reminder.py | ReminderManager（旧内存提醒管理器）用 sleep 后直接发渠道消息；代码引用只有包导出 | 删除旧实现；由 ReminderScheduler（提醒调度器）接管生命周期 |
| bootstrap/application_host.py | ApplicationHost（应用宿主）已装配事务仓储和服务 | 共享同一仓储，追加提醒、通知服务和调度器，显式关闭 |
| channel/web/personal_task_routes.py、app.py | 事务 CRUD、统一错误信封、可注入 host_factory | 保留事务 API；追加提醒/通知 API 与对应异常映射 |
| frontend/src/App.tsx、PersonalTasks.tsx | chat/tasks 切换、事务编辑、删除确认；会话 hook 长驻 | 添加通知工作区、全局通知 hook、独立提醒编辑入口 |
| config/settings.py | personal_tasks 数据库/时区、scheduler.enabled 已存在 | 复用配置，给 enabled 确定运行语义；不增加第二份数据库配置 |
| tests/personal_tasks、tests/channel/web | 真实 SQLite、HTTP、版本竞争和页面测试 | 在现有入口扩展回归，覆盖数据库崩溃提交边界和恢复 |

## 3. 总体改造方案

```text
事务/提醒页面 → Web 路由 → 事务服务或提醒服务 ─┐
APScheduler → 调度器 → 提醒服务 ──────────────┤
                                            ↓
                           同一 SQLite 仓储、一次原子提交
                       事务 + 提醒 + 触发记录 + 站内通知
                                            ↓
通知中心 / 全局轮询 → 通知 API → 通知服务 → 浏览器投递接口
```

SQLite 保有业务状态。APScheduler 每秒唤醒一个扫描作业，周期的下次日期由其日历触发器计算；业务提醒不再注册一份独立持久作业。这样事务改期和提醒改期都只更新同一数据库。恢复流程直接处理数据库中仍待触发的到期记录，原子推进水位及下一时刻。

合并开发后，触发记录与站内通知可在同一提交内生成，省去 #61 到 #62 的跨提交消费空窗。浏览器投递是其后的独立副作用，采用一次认领、明确结果的接口，故障未知时不自动重弹。具体去重、恢复与删除语义见 spec §3—§8。

详细设计补齐了未单独讨论的实现规则，见 spec §2。日期型截止的相对提醒由用户选择当地时刻；手工新建/修改产生的过去时刻被拒绝；修改事务截止使既有相对提醒过期时暂停该提醒并提示。30 秒以上扫描间隔视为恢复，浏览器断线恢复只刷新历史。上述均属于设计决定，不能在实现中另改默认值。

## 4. 分阶段实施

### 阶段一　规则、模型与接口契约

前置依赖为本 spec。新增 `personal_tasks/reminder_models.py`、`reminder_rules.py`、`reminder_service.py`、`notification_models.py`、`notification_service.py`，修改 `repository.py` 与 `pyproject.toml`。实现 ReminderRule（提醒规则）、Reminder（提醒实体）、ReminderOccurrence（触发记录）、Notification（通知）、ReminderService（提醒业务服务）、NotificationService（通知业务服务）及仓储方法，按 spec §2—§4 定义输入输出。加入 `APScheduler>=3.11.3,<4`，复用现有 tzdata。

增加 `tests/personal_tasks/test_reminder_rules.py`、`test_reminder_service.py`、`test_notification_service.py`，必要测试替身放对应测试文件。验收涵盖月末/闰年、时区、过去时刻拒绝、截止类型、暂停/恢复和唯一触发标识；接口具备完整参数，不依赖 Web 或模型。

Commit 为 `feat(assistant): define reminder and notification contracts`。

### 阶段二　持久化与事务联动

依赖阶段一。修改 `personal_tasks/sqlite_repository.py` 的 initialize/_connect/replace/delete 并实现新增仓储方法；修改 `service.py` 的 update，使截止联动计算随原子 replace 提交。按 spec §3—§5 建表、索引、外键及 v1→v2 迁移。原有仓储替身同步接受 replace 的新增可选参数。

新增 `tests/personal_tasks/test_reminder_repository.py`、`test_notification_repository.py`；扩展已有 `test_service.py`、`test_sqlite_repository.py`。验收要求旧数据完整保留、两个连接竞争仅一个提交、完成与到期竞争符合提交顺序、触发与通知同成败、删除清理摘要成员、截止联动不覆盖并发提醒更新。按 spec §9 注入提交前失败并重启读取，证明无半写入。

Commit 为 `feat(assistant): persist reminders and notifications atomically`。

### 阶段三　调度、恢复与宿主生命周期

依赖阶段二。新增 `scheduler/reminder_scheduler.py`，实现 ReminderScheduler（提醒调度器）的 start/stop/_tick；修改 `scheduler/__init__.py` 导出新类型、删除 `scheduler/reminder.py`。修改 `bootstrap/application_host.py` 的 __init__/initialize/shutdown 和服务属性，复用现有配置。调用方调整见 spec §6。

新增 `tests/personal_tasks/test_reminder_scheduler.py`，扩展 `tests/runtime_v2/test_phase2_application_host.py`。验收为启动先恢复后就绪、停机多规则只一条摘要、连续重启不重复摘要、睡眠恢复、关闭等待正在提交的扫描、scheduler.enabled=false 的持久管理行为，以及错误后下次扫描恢复。删除前全仓搜索旧类/旧导入，清除 README 对旧实现的引用。

Commit 为 `feat(assistant): run durable reminder scheduling and recovery`。

### 阶段四　HTTP 与客户端投递契约

依赖阶段二、三。新增 `channel/web/reminder_schemas.py`、`reminder_routes.py`、`notification_schemas.py`、`notification_routes.py`；修改 `app.py:create_app` 和 `schemas.py:WebErrorCode`（Web 错误码）。实现 spec §7 的全部端点、游标和错误映射。现有事务请求/响应保持兼容，通过新列表接口获取提醒。

新增 `tests/channel/web/test_reminders.py`、`test_notifications.py`，扩展已有事务接口测试的联动断言。验收包括严格输入、404/409/422/500、分页无漏读、同一通知多标签认领只一方成功、结果回报幂等、删除后反馈，以及真实 SQLite 的端到端调用。

Commit 为 `feat(web): expose reminder and notification APIs`。

### 阶段五　页面与浏览器通知

依赖阶段四。修改 `frontend/src/{types.ts,api.ts,App.tsx,PersonalTasks.tsx,PersonalTaskEditor.tsx,styles.css}`；新增 `ReminderEditor.tsx`、`TaskReminders.tsx`、`useTaskReminders.ts`、`NotificationCenter.tsx`、`useNotifications.ts`、`notificationDelivery.ts`。职责和函数参数按 spec §8。事务创建后才能打开提醒管理；正常通知点击定位事务，摘要点击显示成员。

新增 `ReminderEditor.test.tsx`、`TaskReminders.test.tsx`、`NotificationCenter.test.tsx`、`useNotifications.test.tsx`、`notificationDelivery.test.ts`，扩展 `PersonalTasks.test.tsx`、`App.test.tsx`。验收涵盖多条提醒编辑与冲突草稿、日期规则、读状态、断线提示、多标签不重弹、权限拒绝、历史不弹、当前页面切换不卸载全局通知 hook。完成 TypeScript 构建和现有前端测试。

Commit 为 `feat(gui): add reminder management and notification center`。

### 阶段六　完整验收与文档收敛

依赖所有前阶段。更新 README.md 的主链、模块状态和目录，更新 docs/docker.md、根及 docker/config.example.yaml 的调度和通知说明。实际开发 PR 描述明确 #62 渠道验收调整为浏览器系统通知，#63 包含月/年。本次设计交付不修改远程 issue。

按 spec §9 完成 Docker 浏览器人工验收，更新这两份文档中的实现状态和实际验证结果；新增发现只修本目标回归。旧调用归零、旧配置可加载、旧数据库迁移通过后完成清理。

Commit 为 `test(assistant): verify reminder lifecycle and document delivery`。

## 5. 完成门槛

### 本次实际交付记录（2026-11-19）

- 六阶段 commit：`7ab38d5`、`1038a7d`、`ba6a506`、`dbb212d`、`2272046`，以及本阶段文档交付提交。
- 静态验证：`.venv\Scripts\python.exe -m compileall -q src/dotclaw/personal_tasks src/dotclaw/scheduler src/dotclaw/bootstrap src/dotclaw/channel/web`；`frontend` 下 `npm run build`；`git diff --check`。
- 动态验证依用户明确要求未执行；SQLite 迁移、扫描恢复、HTTP、浏览器通知和 Docker 流程不能据此宣称运行验收通过。
- 当前运行限制：同一 SQLite 数据目录只能由一个应用进程管理。浏览器通知需要页面打开并获授权；认领后浏览器副作用未知时不自动重弹。

Windows 测试先执行 `.venv\Scripts\python.exe -c "import sys; print(sys.executable)"`，随后使用该解释器运行 pytest。目标测试为 `tests/personal_tasks`、`tests/channel/web`、宿主生命周期相关测试；新增测试均放已收集目录。前端在 frontend 运行 `npm test`、`npm run build`。Docker 内使用 `/opt/venv/bin/python`，沿用现有 compose 工作流，不删除数据卷。

最终必须验证从界面设置提醒到站内生成、浏览器展示、点击回到事务的完整流程，重启汇总和结束后停发，以及 v1 数据兼容。浏览器接受通知请求只代表 accepted，不代表用户已看见；系统关闭通知/免打扰造成的展示差异须在验证记录中如实描述。纯文档阶段以文档覆盖与一致性核对验收，不声称实现测试通过。

## 6. 2026-11-21 基础能力收口

目标是让下一阶段的自然语言事务入口复用确定性业务服务。现有服务已独立于会话，但缺少有界条件查询、统一提醒读取和准确的联动结果；部分边界校验依赖 HTTP。详细契约集中在 spec §11，本节不重复字段规则。

沿用本 PR，依赖已完成的提醒持久化和页面功能，分三个提交阶段：

1. 后端：修改 personal_tasks 下 models/repository/sqlite_repository/service/reminder_rules/reminder_service 以及必要的 Web schemas/routes；增加查询、快照聚合读取、事务提交内产生的提醒变化结果，统一直接服务调用的输入校验。同步相关仓储替身与 tests/personal_tasks、tests/channel/web。验收查询边界、原子联动结果、直接调用错误与 HTTP 映射一致及既有行为兼容。
2. 前端：修改 types/api/usePersonalTasks/PersonalTasks 及必要样式和测试；共用查询条件，支持加载更多，以列表自带摘要替代逐事务请求，明确展示事务修改造成的提醒变化。验收条件切换重置分页、迟到响应不覆盖新条件、分页错误可重试和现有提醒管理兼容。
3. 验收与文档：独立检查关键差异、针对性动态测试及前端构建；实际 Docker 验证非空查询、详情和联动响应，更新 README 和本设计中的实际结果。任何测试临时数据只清理本次创建的记录。

不纳入自然语言解析、消歧确认、助理工具注册、工具重试防重复、事务实例/打卡以及新的通知渠道。事务创建的原子性不等于请求幂等，后者留给下一阶段结合运行时契约决定。
