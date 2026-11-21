# PA-02 提醒与通知详细设计

基线为 `feat/assistant@98a6b99`，2026-11-19。设计与实现已完成；动态运行验证因用户要求未执行。实施记录见 [plan.md](plan.md)。本文的方法签名、字段、默认行为与顺序为实现契约。

## 1. 总体架构与目标目录

复用 PersonalTask（个人事务）、PersonalTaskService（事务业务服务）、PersonalTaskRepository（事务仓储协议）、SqlitePersonalTaskRepository（SQLite 事务仓储）和 ApplicationHost（应用宿主）。事务、提醒和通知属于同一数据库的一致性边界。仓储处理短事务，规则函数负责时间计算，服务提供业务入口，调度器只决定何时扫描。Runtime、会话和 LLM 不参与提醒。

```text
src/dotclaw/
  personal_tasks/
    __init__.py                  保留
    models.py                    保留，复用截止及逾期计算
    service.py                   修改，传递截止联动规则
    repository.py                修改，追加契约与异常
    sqlite_repository.py         修改，迁移和原子业务写入
    reminder_models.py           新增，提醒规则/实体/触发记录
    reminder_rules.py            新增，规则校验和下一时刻
    reminder_service.py          新增，提醒应用入口
    notification_models.py       新增，通知/成员/投递记录
    notification_service.py      新增，查询、已读和投递认领
  scheduler/
    __init__.py                  修改，导出新调度器
    reminder.py                  删除，旧内存定时与直发消息
    reminder_scheduler.py        新增，APScheduler 生命周期
  bootstrap/application_host.py 修改，装配和关闭
  config/settings.py            保留已有配置结构
  channel/web/
    app.py、schemas.py           修改，注册路由和错误码
    personal_task_routes.py      保留，原调用签名兼容
    personal_task_schemas.py     保留
    reminder_routes.py           新增
    reminder_schemas.py          新增
    notification_routes.py       新增
    notification_schemas.py      新增
frontend/src/
  types.ts、api.ts、App.tsx、styles.css            修改
  PersonalTasks.tsx、PersonalTaskEditor.tsx       修改
  usePersonalTasks.ts、Dialog.tsx                 保留
  ReminderEditor.tsx、TaskReminders.tsx            新增
  useTaskReminders.ts、NotificationCenter.tsx      新增
  useNotifications.ts、notificationDelivery.ts    新增
pyproject.toml                                   修改，APScheduler 3.x 依赖
README.md、docs/docker.md、config.example.yaml、docker/config.example.yaml
                                                修改，开发完成时同步
```

测试文件逐项见 plan §4，各目录仍由现有 pytest 配置收集。新前端文件沿用 Vitest 收集方式。旧 scheduler 没有持久数据，删除无需数据转换。

## 2. 确定的产品与时间规则

### 2.1 基础规则

一件事务有零至多条提醒。提醒独立于截止，触发不修改事务状态。只允许 pending/in_progress 的事务创建、修改或恢复提醒。结束事务原子取消所有 active/paused 提醒；重开不恢复旧提醒。取消、fired、missed 状态均为终态，再提醒须创建新条目。暂停仅用于周期提醒，单次通过修改时间或取消处理。

提醒规则为判别联合，字段按 kind 严格拒绝多余值。

| kind | 必填字段 | 定义 |
| --- | --- | --- |
| at | at，带偏移 RFC3339 | 一次性绝对时刻，保存 UTC |
| before_deadline | offset_minutes，非负整数 | 时刻截止减去分钟数，零表示截止时提醒 |
| before_date | days_before，非负整数；local_time，HH:mm | 日期截止向前若干个当地日，在用户选择的时刻提醒 |
| daily | local_time | 每天 |
| weekly | weekdays，去重升序的 0..6 非空列表；local_time | 周一为 0，可多选 |
| monthly | day，1..31；local_time | 每月该日；缺少该日跳过 |
| yearly | month，1..12；day；local_time | 每年该月日，按闰年 2000 校验月日可成立；2 月 29 日有效 |

before_deadline 只用于 datetime 截止，before_date 只用于 date 截止。日期型必须显式选提醒时刻，禁止偷偷使用午夜或默认上午。前端分别显示“提前多少分钟/小时/天”和“提前多少天、当天几点”；后端统一保存表中单位。相对提醒只产生一次触发。周期规则与截止独立，无结束日期输入，事务状态控制结束。

所有新建、直接修改的单次计算结果必须严格晚于本次 now，否则 422。周期首次和恢复后的时刻严格晚于 now。暂停跳过暂停期间，恢复不补通知。改期影响未来，已有触发/通知保持原事实。

### 2.2 截止联动、时区与细节决定

修改事务截止时，在同一次数据库写事务内重算其 active 相对提醒。重算结果 <= now 时将该提醒置 paused，reason=deadline_moved_past，允许其修改时间或恢复（仍须未来）；单次仅能因截止联动进入 paused，另一原因 invalid_local_time 见下文。删除截止或切换截止类型不再匹配时取消 active/paused 相对提醒，reason=deadline_removed_or_changed_kind。保存事务后刷新提醒并显示发生的暂停/取消结果。已触发和已错过的相对提醒不重新激活。已 paused 的相对提醒在截止再次变化后仍保持 paused，用户恢复时按最新截止重新计算。

所有本地日历规则在创建时绑定当前 personal_tasks.timezone，存入 reminder.timezone；绝对时刻仍按 UTC。后续修改应用时区仅影响显示和新建规则，已有提醒继续使用其保存时区，页面明确显示规则时区；用户修改规则时重新绑定当前应用时区。事务日期截止的相对规则沿用该提醒时区解释日期。逾期继续调用现有 is_overdue，使用当前应用时区。日期截止在下一当地日开始变为逾期。

APScheduler CronTrigger（日历触发器）计算 daily/weekly/monthly/yearly；DateTrigger（单次触发器）计算单次。所有候选经 UTC 比较。夏令时不存在的当地时刻跳过；回拨重复时刻只取 fold=0，一次当地日历时刻只触发一次。通过 UTC→当地时间往返验证候选，拒绝/跳过无效候选；单次 before_date 落在不存在的时刻则 422，截止联动遇到此情形暂停、reason=invalid_local_time。周期候选的过滤放统一函数，不由路由/调度器各自实现。

上述时间细节是本次设计补齐的确定规则。长期周期任务实例、打卡、月底自动提前到最后一天均不在范围。

## 3. 数据结构、状态与持久化

### 3.1 reminder_models.py

所有实体为冻结 dataclass，规则用 Literal 判别的类型别名 ReminderRule（提醒规则联合），按 §2 字段定义各分支 TypedDict（带结构的字典），无需额外规则继承树。ReminderStatus（提醒状态枚举）包含 active/paused/cancelled/fired/missed。

Reminder（提醒实体）字段为 id:str、task_id:str、rule:ReminderRule、timezone:str、status:ReminderStatus、reason:str|None、next_fire_at:datetime|None、version:int、created_at:datetime、updated_at:datetime。所有 datetime 为 aware UTC。version 从 1 起，每次规则/状态/next_fire_at 的修改加一。active 必须有 next_fire_at，其他状态必须为空；fired/missed 仅用于单次规则。

ReminderOccurrence（提醒触发记录）字段为 id:str、reminder_id:str、task_id:str、reminder_version:int、scheduled_at:datetime、processed_at:datetime、outcome:Literal['fired','missed']、missed_until:datetime|None。唯一键为 (reminder_id,reminder_version,scheduled_at)。missed 记录描述该版本从 scheduled_at 到 missed_until 的错过区间，周期停机十年也只存一条区间记录，不枚举每一次日期，不提供不准确的错过次数。恢复后规则推进至严格晚于 missed_until 的首个时刻。

### 3.2 notification_models.py

Notification（通知实体）字段为 id:int、kind:Literal['reminder','recovery_summary']、occurrence_id:str|None、created_at:datetime、read_at:datetime|None、title:str、body:str、members:tuple[NotificationMember,...]。NotificationMember（通知关联事务）字段为 task_id、title_snapshot、missed:bool、overdue:bool。单次普通提醒有一个成员，摘要按 task_id 去重。摘要正文从成员统计派生，避免删除事务后残留旧数量；普通标题正文保存触发时快照。摘要详情返回成员及事务当前状态，已完成者显示当前状态但保留摘要历史标记。

NotificationDelivery（通知投递记录）字段为 notification_id:int、channel:str、claim_id:str、status:Literal['attempted','accepted','failed']、attempted_at:datetime、reported_at:datetime|None、error:str|None。当前 channel 唯一支持 browser。attempted 表示已认领、最终展示结果未知；accepted 仅表示浏览器 API 接受。已读与投递状态独立。

NotificationPage（通知页）字段为 items、next_before_id:int|None、unread_count:int、watermark:int。NotificationChanges（通知增量页）字段为 items、next_after_id:int、has_more:bool、unread_count:int、watermark:int。所有计数和页内数据在同一个读事务中读取。

### 3.3 sqlite_repository.py 的版本 2 表

复用现有 database_path；每个连接启用 foreign_keys=ON 和 synchronous=FULL，沿用 5 秒 busy timeout、短连接和 asyncio.to_thread。时间统一编码为固定微秒 UTC Z 字符串，布尔存 0/1。下列“必填”列均 NOT NULL。

| 表 | 列和约束 |
| --- | --- |
| personal_tasks | 原表及字段保留 |
| task_reminders | id TEXT PK；task_id FK personal_tasks ON DELETE CASCADE；rule_json TEXT；timezone TEXT；status TEXT CHECK 枚举；reason TEXT 可空；next_fire_at TEXT 可空；version INTEGER>=1；created_at/updated_at TEXT。CHECK active 与 next_fire_at 非空等价 |
| reminder_occurrences | id TEXT PK；reminder_id FK task_reminders ON DELETE CASCADE；task_id FK personal_tasks ON DELETE CASCADE；reminder_version INTEGER；scheduled_at/processed_at TEXT；outcome CHECK fired/missed；missed_until TEXT 可空；UNIQUE(reminder_id,reminder_version,scheduled_at) |
| notifications | id INTEGER PRIMARY KEY AUTOINCREMENT；kind TEXT CHECK；occurrence_id TEXT 可空 UNIQUE FK reminder_occurrences ON DELETE CASCADE；created_at TEXT；read_at TEXT 可空；title/body TEXT。普通通知要求 occurrence_id 非空，摘要要求为空 |
| notification_members | notification_id FK notifications ON DELETE CASCADE；task_id FK personal_tasks ON DELETE CASCADE；title_snapshot TEXT；missed/overdue INTEGER CHECK 0/1；PRIMARY KEY(notification_id,task_id) |
| notification_deliveries | notification_id FK notifications ON DELETE CASCADE；channel TEXT；claim_id TEXT UNIQUE；status TEXT CHECK；attempted_at TEXT；reported_at/error TEXT 可空；PRIMARY KEY(notification_id,channel) |
| reminder_scan_state | id INTEGER PK CHECK(id=1)；last_scan_at TEXT；timezone TEXT。记录最近已成功提交的扫描及当次应用时区 |

索引为 task_reminders(status,next_fire_at)、task_reminders(task_id)、reminder_occurrences(task_id,processed_at)、notification_members(task_id)、notifications(read_at,id)。JSON 必须按 ReminderRule 解码校验，损坏报存储错误，不静默替换默认规则。

初始化在 BEGIN IMMEDIATE 内读取 user_version。版本 0 沿用“已有用户表则拒绝”的检查，先创建原事务表，再执行版本 1 的升级步骤；版本 1 创建新增表与索引；最后设 user_version=2 并提交。版本 2 检查所需列，其他版本明确拒绝。迁移保留原 id/version/时间字段，不给旧事务自动创建提醒或通知。首次调度看到没有 scan_state 时，只抑制单独由历史逾期触发的摘要；仍处理实际存在的到期提醒（例如此前关闭调度时创建的提醒）。有错过提醒就按恢复规则生成摘要、带入当前逾期事务；完全没有到期提醒则只写当前水位。事务中断整次回滚；不提供反向迁移，回退应用需要使用升级前数据库备份。备份属于开发上线步骤，由实施者先停写后复制。

## 4. 业务服务及规则接口

### 4.1 reminder_rules.py

纯函数签名如下，均不读写数据库。

```python
def validate_rule(rule: ReminderRule, task: PersonalTask, zone: ZoneInfo) -> None: ...
def next_fire_at(rule: ReminderRule, task: PersonalTask, zone: ZoneInfo,
                 after: datetime) -> datetime | None: ...
def reschedule_relative(reminder: Reminder, task: PersonalTask,
                        now: datetime) -> Reminder: ...
```

validate_rule 校验结构、边界和截止匹配；next_fire_at 构造上述 APScheduler trigger，返回严格 > after 的首个有效 UTC 时刻；单次已过去返回 None。周期按当地日期过滤 DST 候选，推进至候选之后再次查询；没有候选返回 None，由创建/恢复入口作为 422 处理。reschedule_relative 按 §2.2 返回 version+1 的对象，保留 created_at，只更新必要的规则派生字段。仅当截止实际变化时调用。

### 4.2 reminder_service.py

ReminderService（提醒业务服务）持有 repository:PersonalTaskRepository、timezone:ZoneInfo、clock:Callable[[],datetime]，clock 默认 UTC now。

```python
async def list(self, task_id: str) -> tuple[Reminder, ...]: ...
async def create(self, task_id: str, *, rule: ReminderRule,
                 expected_task_version: int) -> Reminder: ...
async def update(self, task_id: str, reminder_id: str, *, rule: ReminderRule,
                 expected_version: int, expected_task_version: int) -> Reminder: ...
async def change_status(self, task_id: str, reminder_id: str, *,
                        action: Literal['pause','resume','cancel'],
                        expected_version: int, expected_task_version: int) -> Reminder: ...
async def scan(self, *, startup: bool = False) -> None: ...
async def last_scan_at(self) -> datetime | None: ...
```

读取排序 created_at,id。create 生成 UUID；update 仅接受 active/paused，修改后 active 且 next_fire_at 在未来。pause 仅周期 active→paused；resume 接受 paused→active，并重新计算未来时刻；cancel 接受 active/paused→cancelled。终态操作返回 409，不把重复命令误报成功。读取可以读取已结束事务的历史提醒。

服务先读取事务/提醒并校验以提供清晰错误，再传 expected 版本至仓储；写事务内重新核对，防止校验到提交期间被修改。每次提醒修改只增加提醒 version，不增加事务 version。scan 委托仓储 process_due，所有规则和状态在仓储写事务内读取并计算。last_scan_at 委托仓储 get_scan_time，只读返回水位。

### 4.3 service.py 的原事务服务

create/get/list/change_status/delete 对外签名保留。update 仍构造 version+1 的事务，调用 replace 时额外传 now 和应用 timezone。replace 在锁内重读原事务比较截止，对当时实际存在的 active 相对提醒调用 reschedule_relative，避免使用服务层过期的提醒快照。change_status 使用相同 replace，仓储根据目标状态取消 active/paused。事务 version 冲突使全部联动回滚。

### 4.4 notification_service.py

NotificationService（通知业务服务）持有同一 repository 和 clock。

```python
async def list(self, *, unread_only: bool = False,
               before_id: int | None = None, limit: int = 50) -> NotificationPage: ...
async def get(self, notification_id: int) -> Notification: ...
async def changes(self, *, after_id: int, limit: int = 100) -> NotificationChanges: ...
async def mark_read(self, notification_id: int) -> Notification: ...
async def claim_delivery(self, notification_id: int, *, channel: str) -> NotificationDelivery | None: ...
async def report_delivery(self, notification_id: int, *, claim_id: str,
                          status: Literal['accepted','failed'], error: str | None) -> NotificationDelivery: ...
```

mark_read 幂等，只写首次 read_at。claim_delivery 首次生成 UUID，在唯一键约束下插入 attempted；已认领返回 None；不允许历史通知自动重试。report 相同 claim 和结果重报返回现值，不同终态重报 409。error 为截断至 500 字符的可展示错误，accepted 时必须为空。不存在通知 404、错误 claim 409、未知 channel 422。

## 5. repository.py 与 SQLite 方法契约

在现有协议增加下列 async 方法，SQLite 实现每项调用一个对应 `_..._sync` 并用 to_thread 执行。沿用 PersonalTaskStorageError（事务存储错误）；新增 ReminderNotFound（提醒不存在）、ReminderVersionConflict（提醒版本冲突）、InvalidReminderInput（提醒输入错误）、InvalidReminderTransition（提醒状态冲突）、NotificationNotFound（通知不存在）、NotificationDeliveryConflict（投递冲突）。事务相关错误继续使用原异常。

```python
async def replace(self, task: PersonalTask, *, expected_version: int,
                  now: datetime | None = None, timezone: ZoneInfo | None = None) -> PersonalTask: ...
async def list_reminders(self, task_id: str) -> tuple[Reminder, ...]: ...
async def get_reminder(self, task_id: str, reminder_id: str) -> Reminder: ...
async def create_reminder(self, reminder: Reminder, *, expected_task_version: int) -> Reminder: ...
async def replace_reminder(self, reminder: Reminder, *, expected_version: int,
                           expected_task_version: int) -> Reminder: ...
async def process_due(self, *, now: datetime, timezone: ZoneInfo, startup: bool) -> None: ...
async def get_scan_time(self) -> datetime | None: ...
async def list_notifications(self, *, unread_only: bool, before_id: int | None,
                             limit: int) -> NotificationPage: ...
async def get_notification(self, notification_id: int) -> Notification: ...
async def notification_changes(self, *, after_id: int, limit: int) -> NotificationChanges: ...
async def mark_notification_read(self, notification_id: int, *, now: datetime) -> Notification: ...
async def claim_notification_delivery(self, notification_id: int, *, channel: str,
                                      claim_id: str, now: datetime) -> NotificationDelivery | None: ...
async def report_notification_delivery(self, notification_id: int, *, claim_id: str,
                                       status: str, error: str | None, now: datetime) -> NotificationDelivery: ...
```

replace 的可选参数仅兼容旧调用；缺省 now 使用 task.updated_at，缺省 timezone 对相对提醒计算无影响（每条有保存时区）。协议所有原调用保持可用。create/replace_reminder 写锁内检查父事务存在、active 状态与 expected_task_version，再检查提醒版本/归属；冲突整次回滚。数据库只接受服务构造的合法规则，解码存量数据仍须检测损坏。

delete(task_id,expected_version) 保留签名。删除父事务通过外键清理提醒、触发、普通通知和摘要成员；接着删除成员为空的摘要及其投递记录。剩余摘要正文按剩余成员重算，涉及其他事务的摘要保留。提交前不发布任何浏览器动作。

### 5.1 process_due 的原子步骤

1. BEGIN IMMEDIATE，读取扫描水位和 now。首次无水位按 §3.3 处理，继续扫描已有提醒；后续 now 早于水位时不倒退水位、不触发，提交空操作。
2. 恢复模式为首次无水位、startup=true 或 now-last_scan_at>30 秒。30 秒是本 PR 的固定延迟容限；秒级调度抖动按正常触发。配置禁用后重启启用也走恢复模式。
3. 查询 active 且 next_fire_at<=now 的提醒，按 next_fire_at,id 排序，核对事务为 pending/in_progress。读取后无需跨提交租约，写事务就是认领边界。恢复模式写 missed 区间，正常模式写 fired 记录。正常模式周期最多触发一条，直接计算 >now 的下次时刻；容限内多个到期时刻合并为本次一次提醒。
4. 单次置 fired/missed 并清空 next_fire_at，周期保持 active 并推进 next_fire_at，version+1。正常模式同时为每条 fired 记录插入一条普通通知及成员，occurrence_id 唯一约束保证不重复。内容使用此时事务标题，正文包含原定时间和事务截止（如有）。
5. 恢复模式按事务去重收集 missed 成员。另查当前仍 active、is_overdue(now)=true 且 is_overdue(last_scan_at)=false 的事务作为本段新逾期成员，使用水位保存的 timezone 解释旧时点、当前配置解释 now。若 missed 非空，将当前所有 active 逾期事务加入同一摘要，以使“目前有 N 件已逾期”完整；仅新逾期触发时也加入全部当前 active 逾期事务。二者均空则不生成摘要。摘要正文固定为“离线期间有 M 件事务错过提醒，目前有 N 件事务已逾期”；M/N 可为零，成员并集按 ID 去重。
6. 写新水位 now 与当前 timezone，COMMIT。所有触发、通知、推进与水位一起成功；错误 rollback 并上抛。普通扫描不额外为逾期生成通知。

再次重启时已处理的提醒不再到期；原逾期事务在旧水位已逾期，不再单独触发摘要。若出现新的错过提醒，新摘要允许再次包含仍逾期的事务。恢复摘要是这一段恢复的事实，已读状态不影响恢复计算。

### 5.2 通知查询与投递顺序

历史按 id DESC，before_id 为排他上界，limit 1..100。watermark 使用 sqlite_sequence 中 notifications 的已分配最大值，即使尾部通知删除也不倒退。增量按 id ASC、id>after_id，获取 limit+1 判断 has_more；有更多时 next_after_id 为本页末 ID，否则为同一读快照的 watermark。删除产生的 ID 空洞跳过。

认领写事务检查通知存在，插入 (notification_id,browser)；冲突返回 None，首次返回 attempted 记录。客户端拿到 claim 后才调用系统 API。若响应丢失，数据库仍保留 attempted，不做自动重试以避免重复弹窗。客户端显示后结果报告失败保留 attempted，通知中心显示“结果未确认”。本 PR 不承诺浏览器副作用恰好一次。

## 6. scheduler 与宿主

### 6.1 reminder_scheduler.py

ReminderScheduler（提醒调度器）持有 service:ReminderService、scheduler:AsyncIOScheduler（异步调度器）、_stopping:bool 和 _tick_lock:asyncio.Lock（扫描互斥锁）。

`async start() -> None` 先 await service.scan(startup=True)，成功后启动 APScheduler，注册唯一 interval 作业每秒调用 _tick，id='personal-reminders'、max_instances=1、coalesce=True、misfire_grace_time=None。使用内存 job store；扫描状态已由业务库保存。

`async _tick() -> None` 获取锁，停止中直接返回，否则 await service.scan()。运行期存储错误保留原水位，记录带 traceback 的错误；APScheduler 下次 tick 再试。未知程序异常也报告给 scheduler 的错误事件，不吞掉为成功。持续失败通过提醒列表的 scan_lag_seconds 呈现，日志保留根因。

`async stop() -> None` 先置停止标志并暂停作业，再获取锁等待当前扫描结束，最后 shutdown(wait=False)。不能取消正在 to_thread 中提交的 SQLite 写入后立即声称关闭成功。最大等待由单次 SQLite 超时和本地计算决定；现有单用户数据量下全批处理，不引入队列分片。

### 6.2 application_host.py

新增 `_reminder_service`、`_notification_service`、`_reminder_scheduler`，属性 reminder_service、notification_service 返回已初始化实例。initialize 在已有 task_repository.initialize 之后装配三个服务；scheduler.enabled=true 时 await scheduler.start，成功才输出宿主就绪。false 时允许管理提醒和查询通知，但不扫描；API 返回调度关闭状态。首次启动错误沿现有 build 的失败清理路径退出，不退化到内存。

shutdown 在其他共享资源关闭前 await reminder_scheduler.stop，再清空引用。Web lifespan 继续使用既有 host_factory，无新增全局单例。旧宿主测试替身增加服务属性仅在访问新增路由时需要；已有事务路由不依赖提醒服务属性。

## 7. HTTP 边界

### 7.1 reminder_schemas.py 与 reminder_routes.py

ReminderRulePayload（提醒请求规则）用 Pydantic 判别联合严格实现 §2，局部模型按 AtRulePayload（绝对时间请求）、BeforeDeadlineRulePayload（时刻偏移请求）、BeforeDateRulePayload（日期偏移请求）、DailyRulePayload（每日请求）、WeeklyRulePayload（每周请求）、MonthlyRulePayload（每月请求）、YearlyRulePayload（每年请求）命名。字段禁止 extra，整数禁止 bool，时间 HH:mm 严格两位。CreateReminderRequest（创建提醒请求）包含 rule/expected_task_version；UpdateReminderRequest（更新提醒请求）另含 expected_version；ReminderStatusRequest（提醒状态请求）包含 action 和两个 expected 版本。ReminderResponse（提醒响应）对应 Reminder 全部字段。

| 方法与路径 | 输入/输出与路由调用 |
| --- | --- |
| GET /api/v1/personal-tasks/{task_id}/reminders | list_reminders → service.list；返回 items、timezone、scheduler_enabled、last_scan_at、scan_lag_seconds |
| POST 同路径 | create_reminder → create；201 ReminderResponse |
| PUT 同路径/{reminder_id} | update_reminder → update；200 |
| POST 同路径/{reminder_id}/status | change_reminder_status → change_status；200 |

last_scan_at 通过服务 last_scan_at() 读取，首次无水位为 null，lag 为 now-last_scan_at 的非负秒数，无水位为 null。配置 enabled=false 优先显示“提醒调度已关闭”，enabled=true 且 lag>30 显示“提醒调度延迟”。路由调用 service.list 和 service.last_scan_at 后包装结果，禁止直接读仓储。

### 7.2 notification_schemas.py 与 notification_routes.py

NotificationResponse（通知响应）对应 Notification，加 deliveries 列表；成员加 current_status 和 current_is_overdue，由同一次读取关联事务计算。历史已删除成员不会返回。NotificationReadRequest 无 body；ClaimDeliveryRequest（投递认领请求）仅 channel='browser'；ReportDeliveryRequest（投递结果请求）包含 claim_id/status/error。列表响应字段按 §3.2。

| 方法与路径 | 行为 |
| --- | --- |
| GET /api/v1/notifications?unread_only=false&before_id=&limit=50 | service.list |
| GET /api/v1/notifications/changes?after_id=0&limit=100 | service.changes；固定路由在 /{id} 之前注册 |
| GET /api/v1/notifications/{id} | service.get |
| POST /api/v1/notifications/{id}/read | service.mark_read；200 |
| POST /api/v1/notifications/{id}/deliveries/claim | service.claim_delivery；200 返回 claim 或 null |
| POST /api/v1/notifications/{id}/deliveries/report | service.report_delivery；200 |

id 为正整数，after_id>=0。新增错误码 reminder_not_found(404)、reminder_version_conflict(409)、reminder_invalid_transition(409)、notification_not_found(404)、notification_delivery_conflict(409)。输入错误复用 invalid_request(422)，存储错误复用 personal_task_storage_failed(500)。事务版本冲突沿用旧错误码。路由只做请求映射与响应转换，不调用 SQLite 或 APScheduler。所有网络入口沿用当前本地应用访问模型，不增加未确认账户体系。

## 8. 前端模块详细设计

### 8.1 types.ts、api.ts

按 HTTP 契约新增 ReminderRule（前端提醒规则）、Reminder（前端提醒实体）、NotificationItem（前端通知）、NotificationDelivery（前端投递记录）等结构。api.ts 复用 requestJson/ApiError（API 错误），新增 listTaskReminders(taskId,signal?)、createTaskReminder(taskId,rule,taskVersion)、updateTaskReminder(taskId,id,rule,version,taskVersion)、changeTaskReminderStatus(taskId,id,action,version,taskVersion)、listNotifications(options,signal?)、getNotification(id)、getNotificationChanges(afterId,signal?)、markNotificationRead(id)、claimNotificationDelivery(id)、reportNotificationDelivery(id,claimId,status,error)。签名返回对应 HTTP Promise，全部 ID URL 编码。

### 8.2 TaskReminders.tsx、ReminderEditor.tsx、useTaskReminders.ts

TaskReminders({taskId,onClose})（事务提醒管理组件）打开时读取最新事务及提醒；显示提醒规则、时区、状态/原因和下一时刻，提供新增/编辑/暂停/恢复/取消。结束事务仅可查看。ReminderEditor({task,reminder,timezone,pending,onSave,onClose})（提醒编辑组件）提供 §2 的七种规则表单及月份跳过提示。at 沿用现有显式偏移时间输入，不擅自以浏览器时区解析当地时刻。相对规则随当前截止类型展示，日期型要求输入 local_time。

useTaskReminders(taskId) 返回 task/items/timezone/loading/pending/error/reload/create/update/changeStatus。复用现有 usePersonalTasks 的请求序号、AbortController（请求取消控制器）、单次 mutation 锁和冲突保留草稿交互方式，不抽取全局通用 CRUD hook。每次 mutation 后重新读取事务和提醒。409 时展示重新读取并明确确认替换草稿，不自动重发旧请求。因截止改期造成 paused/cancelled 的 reason 显示中文解释。

PersonalTasks（事务页面）增加每条事务的“提醒”按钮和 TaskReminders 对话框；创建事务成功后用户可直接打开提醒设置。PersonalTaskEditor（事务编辑组件）在有截止且被清空/变更类型时显示关联提醒会取消的说明；保存后读取提醒并展示暂停/取消结果。删除确认说明同时删除提醒和通知。页面新增可选 focusTaskId、notificationId 参数，由 App 传入，点击摘要时读取通知成员并显示对应事务子集，仍可打开事务和提醒；清除定位恢复原筛选。定位已删除事务时显示“事务已删除”。

### 8.3 NotificationCenter.tsx、App.tsx

NotificationCenter({onOpenTask,onOpenSummary})（通知中心组件）分页展示全部/未读、详情、已读按钮、关联事务及 browser 投递状态。详情打开后调用 mark_read，失败明确显示并保留未读。普通提醒显示原定时间，摘要显示成员及当时 missed/overdue 标记和当前状态。投递 attempted 显示“结果未确认”，accepted 显示“已交给浏览器”，failed 显示错误。

App（应用根组件）增加 notifications 工作区和未读数入口。useNotifications 在 App 根长驻，切换 chat/tasks/notifications 不卸载；原会话工作区 hook 保留。onOpenTask(id) 切到 tasks 并设置 focusTaskId；onOpenSummary(id) 切到 tasks 并设置 notificationId。浏览器通知点击调用相同导航并 window.focus，不通过任意外部 URL 跳转。

### 8.4 useNotifications.ts

`useNotifications(delivery: NotificationDeliveryPort, onNavigate: (item: NotificationItem) => void)`（全局通知 hook）返回 unreadCount、connectionError、permission、enableSystemNotifications、refresh。先 listNotifications 取得 watermark 作为当前订阅起点，仅刷新历史与未读，不弹旧消息。之后每 5 秒串行调用 changes(cursor)，has_more 时立即读下一页，禁止重叠请求。只有页面订阅后获取的新通知可以投递。

轮询失败保留可见连接错误并继续下一轮重试；第一次恢复成功先重新取得 watermark 和未读数，断线期间积压仅进通知中心。前端检测两次轮询间隔>30秒也按重连处理，避免浏览器休眠后刷屏。恢复摘要若在正常连接期间到达，按新通知弹一次；刷新页面后它属于历史，仅显示未读。页面卸载停止 timer 并取消在途读取。全局通知 hook 和中心列表分别按需刷新；读取通知后 refresh 更新未读数。

只有用户点击“启用系统通知”才请求权限。系统不支持、安全上下文不满足、权限被拒绝时显示具体原因，站内功能继续。授权成功从当前 watermark 开始处理未来消息，不补弹历史。初次页面加载若浏览器 permission=granted，可直接处理未来消息。

### 8.5 notificationDelivery.ts

NotificationDeliveryPort（客户端系统通知投递接口）定义如下，由 BrowserNotificationDelivery（浏览器通知适配器）实现，App 显式实例化注入，无插件工厂。

```typescript
interface NotificationDeliveryPort {
  permission(): "unsupported" | "default" | "denied" | "granted";
  requestPermission(): Promise<"default" | "denied" | "granted">;
  deliver(item: NotificationItem, onClick: () => void): Promise<void>;
}
```

deliver 直接创建浏览器 Notification（浏览器系统通知对象），title/body 使用服务端内容，tag='dotclaw-'+id，注册点击导航；构造成功 resolve，异常 reject。调用方先检查 granted，再 POST claim；claim=null 跳过，得到 claim 才 deliver，随后报告 accepted/failed。网络失败只显示连接错误，不凭空认定发送失败或再次认领。多标签通过数据库唯一认领避免重复系统弹窗。当前投递接口的前端位置对应浏览器实际副作用，后续桌面端可消费同一 HTTP 通知/认领契约，本 PR 不增加无调用方的后端 send 空接口。

## 9. 完整流程、异常与验证

### 9.1 正常触发

页面创建事务 → POST 提醒 → 规则计算未来时间 → 仓储检查父版本并保存 → 每秒 tick → 写锁读取到期提醒 → 唯一触发记录 + 规则推进 + 通知 + 水位一次提交 → 页面增量读取 → 浏览器认领 → 系统 API → 报告结果。截止、事务、提醒修改与 tick 以数据库提交先后生效。事务取消先提交则后续无触发；触发先提交则保留既有通知，取消只影响未来。

### 9.2 停机、失败与恢复

启动扫描或运行间隔超过 30 秒 → 读取尚未处理的到期规则 → 写 missed 区间、推进周期、结束单次 → 合并当前相关事务 → 写一条摘要和新水位 → 提交。提交前进程结束则全部回滚，下次处理同一批；提交后结束则记录已推进，下次不会重复摘要。故障期间数据库写失败时不伪造已处理水位，恢复后走同一路径。

浏览器认领与系统显示无法同一事务，认领后崩溃保留 attempted，不自动重弹；通知中心仍可查阅。已读 API 可重复调用。完成事务不会清除历史；永久删除级联清理。删除与投递同时发生时，claim 前删除返回 404；claim 后已发送的系统弹窗可能继续存在，点击时处理 404，report 收到 404 结束该回报且不重建通知。

### 9.3 兼容与禁止行为

原事务 API、版本、状态矩阵与逾期函数保持既有语义。新增提醒的版本不改变事务版本。原配置 scheduler.enabled 开始实际控制调度，保留公开配置字段。原数据库迁移只加表，不为已有事项猜测提醒。所有新注释使用中文。

禁止内存 sleep 保存业务提醒、禁止另外持久化 APScheduler 作业副本、禁止在数据库提交前发送系统通知、禁止把 accepted 记为已读、禁止恢复逐条弹历史提醒、禁止捕获存储错误后写默认成功、禁止因通知调用进入 Runtime/LLM。仅使用一个应用进程管理同一数据目录，沿用现有 Docker 部署约束。

### 9.4 必须通过的验收案例

1. v1 数据库升级后原 CRUD/冲突/日期截止行为通过，升级中途失败回滚；版本 2 再初始化无重复数据。
2. 同事务三条提醒分别改期/取消/触发，提醒版本竞争和父事务版本竞争返回正确 409；完成事务与扫描两个连接竞争无部分联动。
3. 月 31 日跨 4 月、年 2 月 29 日跨平年、周多选、DST 空洞/重叠验证 next_fire_at；每年 2 月 30 日和非法单次日期被拒绝。
4. 日期型截止相对提醒必须显式选时刻；截止前移至过去暂停、移除截止取消、已触发提醒不复活。
5. 在 occurrence 插入后、notification 插入后、水位更新前注入异常，确认重启后各实体同成败；重复扫描不重复普通通知。
6. 停机期间多事务/多周期只生成一条摘要，M 按事务去重、N 为当前全部未结束逾期；无截止但错过提醒仍列入；连续重启零重复；仅新逾期无提醒也能触发摘要。
7. 暂停期间不补，恢复从未来开始；scheduler.enabled=false 可管理、不可触发，重新启用按恢复处理；shutdown 等待正在写入的 tick。
8. HTTP 真实仓储验证游标删除空洞、分页、增量顺序、已读幂等、重复 claim、结果冲突、事务删除后的摘要成员清理。
9. 两个浏览器标签读取同一新通知，仅一个 claim 成功；权限拒绝时站内完整；刷新/断网重连/睡眠不重弹历史；普通和摘要点击定位正确。
10. Docker 运行中网页后台收到系统通知；关闭网页后站内仍生成，重开只见历史未读；服务停机后恢复看到汇总。浏览器计时节流和 OS 免打扰不按准秒送达验收，后端正常扫描到期至入库以 2 秒内为无负载测试目标。

设计依据包括仓库 README、docs/docker.md、当前个人事务/宿主/Web/前端实现，以及 [APScheduler 3.x 官方指南](https://apscheduler.readthedocs.io/en/3.x/userguide.html)、[浏览器通知文档](https://developer.mozilla.org/en-US/docs/Web/API/Notifications_API/Using_the_Notifications_API)。后两者在本次讨论中已核对。所有实现与测试结果须在开发阶段记录，不将设计契约作为运行证据。

## 10. 2026-11-20 日常使用优化补充

本节记录用户随后确认的使用流程调整，相关条目以本节为准。

- CreatePersonalTaskRequest（创建事务请求）增加可选 `reminders: list[ReminderRulePayload]`，缺省空列表。原客户端继续适用，201 仍返回 PersonalTaskResponse（事务响应）。PersonalTaskService（事务业务服务）的 create 增加 `reminders: tuple[ReminderRule, ...] = ()`，先验证全部规则及未来时刻，再调用 `PersonalTaskRepository.create_with_reminders(task, reminders)`（事务与初始提醒的原子创建）。SQLite 在同一个 BEGIN IMMEDIATE 中插入事务和全部初始提醒，任何失败整次回滚。无初始提醒继续使用既有 create。
- 新建表单允许附一条指定时刻或截止前一天提醒，保存后回列表，明确提示事务或事务及提醒已创建。后续多条/周期提醒仍由提醒管理入口设置。
- 事务列表读取并显示最早活动提醒时刻及额外活动提醒数量。摘要读取失败显示重试，刷新、聚焦和既有定时刷新重验证当前列表摘要，迟到请求不得覆盖新结果。保留“完成”和“提醒”为主操作，其余放更多菜单，删除保留确认步骤。
- 创建表单与提醒编辑器提供“今天15:00”“明天09:00”“截止前1天”。前两者按应用时区计算，今天15:00已过则禁用；截止快捷保存相对规则，时刻截止为 before_deadline 偏移1440分钟，日期截止为 before_date 提前一天当地09:00，用户可继续编辑。
- 通知详情优先展示事项、可读时间和查看事务/摘要主操作，多成员及投递状态放折叠区。列表与详情的已知普通提醒正文统一格式化时间，保留原始持久文本，明确浏览器当地时区；未知正文保持原文。

针对上述调整新增原子创建回滚、创建后回列表、摘要重验证、快捷规则及通知主操作测试。相关后端测试104项、前端测试121项通过；实际 Docker 页面完成新建并附明天09:00提醒、返回列表显示摘要、更多菜单及通知主操作核对。验收临时事项已通过 API 清理。

## 11. 2026-11-21 面向后续入口的基础契约

本节覆盖此前“逐事务加载摘要”的设计；不引入自然语言或模型调用。目录保留既有 personal_tasks、channel/web、frontend/src 分层，修改对应模型、服务、仓储、Web请求响应、事务hook和页面及其测试，不新增基础设施依赖。内部实现方法的最终清单随实现收敛补记。

### 11.1 查询与聚合读取

`models.py` 增加 PersonalTaskQuery（事务查询条件）：filter默认active、keyword默认空、deadline_from/date可空、deadline_until/date可空、overdue_only默认false、offset默认0、limit默认50。limit范围1..100，offset非负且为整数，日期范围非空时必须from<until。keyword去除两端空格后匹配标题和说明，不区分大小写，`%`、`_`是字面字符。

日期范围为应用时区日历日，from包含、until不包含。日期截止按日期比较，时刻截止按该日期的当地零点转换UTC比较；无截止不匹配日期范围。overdue_only复用现有逾期定义，已结束事务永不逾期。先条件筛选、沿用现有deadline_sort_at/created_at/id排序、再分页，返回total、offset、limit及既有timezone/as_of。

PersonalTaskService（事务业务服务）增加 `query(query: PersonalTaskQuery)`。旧 `list(filter)` 保留兼容，HTTP列表改用query；查询输入在服务边界验证，不能依靠Web请求模型才能保证合法。仓储聚合查询在同一SQLite读取事务内获取事务与提醒，形成一致快照；当前单用户规模允许在快照内排序分页，禁止调用方加载全部事务自行筛选。

列表项增加 `reminder_summary: {active_count:int, next_fire_at:datetime|null}`，统计active提醒且取最早next_fire_at，无active时0/null，不混入paused或历史。服务增加 `get_detail(task_id)`，读取同一快照内的事务及完整提醒；GET单项增加reminders字段，旧get保持现有语义。聚合查询与详情不存在时沿用既有异常，不将存储失败当作空结果。

HTTP `GET /api/v1/personal-tasks` 接受filter/keyword/deadline_from/deadline_until/overdue_only/offset/limit。日期使用YYYY-MM-DD。原items/timezone/as_of字段保留，默认列表变为最多50条，客户端必须处理total和加载更多；这属于本次明确的列表兼容调整，其他CRUD端点和版本校验保持。

### 11.2 联动结果与写入顺序

`models.py` 增加 ReminderChange（提醒变化结果）：reminder_id、action（rescheduled/paused/cancelled）、reason（可空）、next_fire_at（可空）、version。PersonalTaskView（事务展示结果）末尾增加默认空元组reminder_changes，不破坏旧位置参数。

修改截止和事务状态时，SQLite持有写锁后校验父事务版本，读取相关提醒，写事务及既有提醒联动，收集本次实际改变的提醒结果，提交成功后一起返回。不可提交后重新读取提醒再猜测变化，也不可比较两个跨事务快照生成反馈。无变化返回空集合；已触发/错过等历史提醒不报告为新取消。过往规则仍生效：相对规则随截止移动、无未来时刻暂停、移除或改变截止类型取消，完成/取消事务结束活动及暂停提醒，重新打开不恢复。

PUT事务与POST状态响应增加 `reminder_changes`，返回已提交的版本和最终状态；其他响应允许空集合。原仓储replace调用方保留兼容入口；新增返回联动结果的方法或返回模型必须同步协议、SQLite实现和测试替身，禁止破坏既有调用。

### 11.3 共用边界与前端调用

服务接收外部入口提供的领域参数，统一检查查询、标题、规则结构/类型/范围及必要版本参数；HTTP仅负责字符串/JSON到领域参数转换与错误码映射。非法输入产生明确领域输入异常，找不到/冲突/无效状态/存储失败保持不同异常。直接服务调用不得接受HTTP会拒绝的非法提醒规则；复用既有规则校验函数，不再复制一套规则。信任通过边界校验的内部对象，不堆叠防御分支。

前端 `types.ts/api.ts` 定义查询、分页元数据、摘要、详情和联动结果；`usePersonalTasks.ts`统一持有条件/分页/在途序号。切换条件从offset0重新读取，加载更多追加同条件页，失败保留已有页并可重试；换条件或刷新后旧请求不得覆盖。列表直接使用服务端摘要，删除为每个可见事务分别读取提醒的摘要请求。显式刷新、定时刷新和聚焦刷新重新验证当前查询，不能混合旧分页与新排序。提醒管理仍使用原独立提醒接口。

`PersonalTasks.tsx`提供关键词、截止日期范围和逾期筛选以及加载更多；使用界面日期的结束日时转换为次日until，标签说明包含该日。事务编辑/结束成功显示实际联动数量和动作，空集合不虚构反馈；冲突保留既有确认逻辑。未来非网页入口直接调用相同服务，禁止绕过服务写SQLite。

### 11.4 验收与禁止行为

使用独立临时数据库验证：直接服务调用创建带提醒事务→条件查询→同快照详情→修改截止返回实际提醒变化→结束后无未来提醒→重启读取保持结果。覆盖时区日期边界、无截止/结束状态、关键词通配符字面匹配、相同排序值分页、非法范围/limit/版本、真实写入失败无半结果，以及HTTP相同错误映射。前端验证查询切换、加载更多、迟到响应、空结果/读取失败和无N+1请求；运行相关后端测试、前端测试与构建，结果另记，不以设计断言替代证据。

不提供批量模糊修改或按标题直接变更事务；不预设模型解析结果、消歧或确认策略；不以自动重试创建来掩盖网络不确定结果。分页跨多次请求不承诺全局快照，数据变化后以刷新重建结果。

### 11.5 文件与方法对应

- `personal_tasks/models.py`：新增 PersonalTaskQuery、ReminderSummary（提醒摘要）、ReminderChange；PersonalTaskList（事务列表结果）增加total/offset/limit和与items同序的reminder_summaries。既有字段顺序保持。
- `personal_tasks/repository.py`：协议增加 `read_task_snapshot() -> tuple[tuple[PersonalTask,...], dict[str,ReminderSummary]]`、`get_detail(task_id) -> tuple[PersonalTask|None, tuple[Reminder,...]]`、`replace_with_effects(task, *, expected_version, now=None, timezone=None) -> tuple[PersonalTask,tuple[ReminderChange,...]]`。原replace仍返回事务，不改成元组。快照为每个事务提供摘要，无活动提醒为0/null；服务直接读取该结果，不重复兜底。
- `personal_tasks/sqlite_repository.py`：对应async方法用既有to_thread模式调用同步实现；read_task_snapshot/get_detail用显式读取事务固定快照，replace_with_effects复用既有写事务，在COMMIT成功后返回事务及变化，replace丢弃变化保持兼容。快照方法不接收查询条件，条件筛选统一在服务query中执行。
- `personal_tasks/service.py`：query校验、筛选排序分页并组合列表；get_detail将仓储不存在映射领域异常；update/change_status调用replace_with_effects并填入view。旧list保留完整列表语义，HTTP有界列表统一走query。
- `personal_tasks/reminder_rules.py`：validate_rule统一七种规则的字段、类型、范围及时间格式；normalize_rule将已验证的at规则转为UTC微秒Z格式，其他规则保持原值，供事务带提醒创建及提醒create/update三个调用方复用；`reminder_service.py`在写入口验证版本和动作后复用规则函数，未来入口无需引用Web模型。
- `channel/web/personal_task_schemas.py`：增加提醒摘要、联动变化、列表项和详情响应；`personal_task_routes.py`把查询参数组装为领域查询，详情调用get_detail，变更序列化view.reminder_changes；沿用现有app异常映射。
- `frontend/src/api.ts/types.ts`：列表请求接收组合查询，响应带total/offset/limit及每项reminder_summary；详情类型增加reminders；修改及状态响应含reminder_changes。
- `frontend/src/usePersonalTasks.ts`：reload(preserveLoaded)从首页刷新并按需恢复已加载范围，loadMore按服务分页追加、按ID去重；条件变化重置页并取消旧请求，序号阻止迟到结果覆盖。保留聚焦和60秒刷新，以列表摘要代替逐事务提醒读取。
- `frontend/src/PersonalTasks.tsx`：提供状态/关键词/日期/逾期组合筛选、加载更多与准确联动反馈。通知定位单独按ID调用getPersonalTask，摘要先读取通知成员再定向读取成员详情；不改变当前查询，不依赖首50条。404显示已删除，其他失败显示可重试错误；新定位或卸载忽略旧结果。
- `frontend/src/useTaskReminders.ts`：详情返回的事务和reminders来自同一快照，提醒端点仍用于读取调度启用和扫描延迟元数据。新增/修改/状态操作后复用现有刷新，TaskReminders完成变更时通知事务列表刷新摘要。

### 11.6 实际验收记录

后端提交 `1d7b2e6`，旧list完整语义修复 `3b2d39d`，前端提交 `5fc65ed`。开发使用项目解释器核对后运行 `tests/personal_tasks tests/channel/web`：109项通过；前端15文件124项通过，TypeScript/Vite构建通过。独立审查发现并闭环旧list截断和通知定位跨分页问题；最终定位测试覆盖首屏外事务、真实404及临时错误重试。随后 `8028f44` 清理未读状态并修正首次定位加载提示，相关11项组件测试和构建再次通过。构建仍有既有大于500kB的chunk提示，测试存在既有Markdown/HTML嵌套警告。

最终 `a78be1a` 同步清除定位错误提示，相关12项组件测试通过，避免清除定位后残留不可重试的错误区域。

主代理通过实际Docker的5173代理完成：创建带日期截止和相对提醒的临时事务→关键词/日期范围/limit组合查询返回摘要→详情读取提醒→改期返回rescheduled→完成返回cancelled→按精确临时标记核对后删除。容器healthy。此次未完成新的浏览器人工验收：浏览器控制工具报告Chrome不可用；不把HTTP验收或组件测试表述为浏览器人工验证。系统通知弹窗不属于本轮变更验收。

### 11.6 后续清理记录

`c24cdc7` 将未消费查询参数的仓储query更名为无参read_task_snapshot，明确筛选/排序/分页属于服务职责；SQLite快照仍提供每个事务的摘要，服务移除重复默认值。normalize_rule替代三处相同的UTC转换。`990bef7` 删除事务hook未被使用的filters/showAll返回入口，以及提醒详情加载中从未执行abort的局部控制器和signal参数。未新增取消机制，未改变提醒详情请求时机。

保留旧service.list/repository.list/replace兼容入口、scheduler公开导出和enabled配置、HTTP与服务各自边界校验、提醒端点的调度状态元数据。不能将这些有职责或已有契约的接口认作废弃代码。此次清理后端定向24项、前端相关13项测试及构建通过，未重跑全量测试或浏览器手测。
