# PA-01 个人事务管理：总体设计与实施计划

状态：设计交付，尚未实现。基于 2026-11-17 本地 master `8c6ec61` 与已确认讨论。
交付方式：默认一个 PR，按下列依赖阶段提交；本次仅落盘文档。
详细设计唯一来源：[spec.md](spec.md)。[范围基线](pa-01-scope.md)保存已确认产品边界。

## 1. 目标、范围与成功条件

让用户在现有 Web 工作台独立创建、编辑、查询、完成、重新打开、取消和删除个人事务，刷新或重启后仍然保留；不选择会话、不调用模型即可操作。

必做：独立事务实体、状态与截止规则、SQLite 持久化、专用仓储接口、版本冲突检查、HTTP API、最小页面及必要验证。同名事务允许存在；防止页面重复提交不等于后端创建幂等。

非目标：调度、提醒、执行记录、通知、自然语言工具、后台执行、来源会话关联、归档、回收站、历史审计、多用户、搜索、批量操作，以及标签、分类、优先级、子任务、附件。后续实际需要时扩展，不预建字段、空表或插件。通用存储整理另行处理。本 PR 不迁移会话/运行/记忆数据，不修改 Runtime 状态机，不放宽现有模型配置启动条件。

## 2. 已核对的现状与差异

| 当前事实与依据 | 目标及改动 |
| --- | --- |
| README 定位为本地单进程 Agent Harness；Scheduler 尚未接入主链 | 新增确定性个人事务能力，不宣称已经具备持久提醒 |
| `bootstrap/application_host.py` 的 ApplicationHost（应用宿主）统一装配服务 | 追加事务仓储初始化与业务服务暴露，复用宿主生命周期 |
| `runtime/application/ports.py` 的 RunRepository（运行仓储接口）保存运行事实与恢复控制 | 不复用其业务契约；新增 PersonalTaskRepository（个人事务仓储接口） |
| `session/session.py` 的 SessionManager（会话管理器）读写 JSON；`memory/storage.py` 的 MemoryStorage（记忆存储）直接使用 SQLite | 既有实现保留；本次数据库只承载个人事务，禁止提取全项目通用存储框架 |
| `channel/web/app.py:create_app` 在 lifespan 持有宿主，已有统一错误信封及请求校验 | 注册事务路由；保持旧 API、SSE 与宿主工厂注入方式 |
| `frontend/src/App.tsx` 持有会话工作区与 SSE 状态；`api.ts` 有请求及错误解析 | 增加事务视图，保持会话 hook 挂载；复用请求与错误处理 |
| `config/settings.py` 没有事务数据库及应用时区配置；依赖未显式声明 tzdata | 新增最小配置与时区数据依赖，支持 Windows 的 IANA 时区 |

本次新增而非替换已有业务链；没有旧模型、数据或生产文件需要删除。未来个人助手调用同一业务服务，本 PR 不实现工具入口。

## 3. 总体方案

```text
事务页面 → HTTP 路由 → PersonalTaskService（个人事务业务服务）
                         ↓
                  PersonalTaskRepository（个人事务仓储接口）
                         ↓
              SqlitePersonalTaskRepository（SQLite 个人事务仓储实现）
                         ↓
                 data/personal-assistant.sqlite3
```

业务服务独占状态规则、时间解释和列表投影；仓储负责持久化、原子版本校验和提交。数据库是事务当前状态的唯一事实源；逾期不持久化。界面收到提交确认后才展示成功；冲突保留草稿、不自动覆盖。

关键取舍：仓储按业务定义，不统一为万能 CRUD；每次数据库操作在工作线程中短连接执行，不引入常驻连接池或后台任务；SQLite 原子提交负责进程中断恢复，不引入运行检查点。具体约束、方法、SQL、HTTP 和界面行为均见 spec §2—§8。

## 4. 分阶段实施与 commit

### 阶段一：业务模型、仓储契约与服务

前置：spec §2、§3 的数据规则已固定。

- 新增 `src/dotclaw/personal_tasks/{__init__,models,repository,service}.py`。
- 实现 PersonalTask（个人事务）、TaskDeadline（事务截止值）、状态与筛选枚举；实现仓储协议及业务异常，详见 spec §3。
- 实现 PersonalTaskService（个人事务业务服务）的 create/get/list/update/change_status/delete；状态迁移、排序、逾期和版本语义按 spec，不分散到路由。
- 新增 `tests/personal_tasks/test_service.py`，测试内提供满足仓储协议的最小替身；不增加生产内存仓储。
- 修改 `pyproject.toml` 的 pytest testpaths，纳入 `tests/personal_tasks`；增加 tzdata（阶段二初始化时需要）。

验收：状态矩阵、完成时间、同名创建、时间精度、日期边界、排序、版本冲突均有确定性测试；业务服务无模型、会话或 Web 依赖。
Commit：`feat(personal-tasks): define task model and application contract`。

### 阶段二：SQLite、配置与宿主装配

前置：阶段一契约；spec §4、§5。

- 新增 `personal_tasks/sqlite_repository.py`，实现 initialize/create/get/list/replace/delete；SQL 事务与失败映射严格遵循 spec §4。
- 修改 `config/settings.py` 的 Config（应用配置）与 _raw_to_config（load_config 的配置映射调用方）；`config/__init__.py` 导出配置类型；修改根目录及 docker 的 `config.example.yaml`。
- 修改 `bootstrap/application_host.py` 的 __init__/initialize，新增 personal_task_service 属性；不修改 runtime_factory 的返回模型。
- 新增 `tests/personal_tasks/test_sqlite_repository.py`、`test_config.py`；修改 `tests/runtime_v2/test_phase2_application_host.py` 的配置夹具，补宿主初始化用例。

验收：临时文件数据库重建仓储实例后数据可读；两个独立仓储实例使用旧版本竞争，仅一项成功；失败事务无半写入；旧配置可加载；初始化失败阻止就绪，不退化到内存。
Commit：`feat(personal-tasks): persist tasks with sqlite and wire host`。

### 阶段三：HTTP 接口

前置：阶段二可使用真实数据库；spec §6。

- 新增 `channel/web/personal_task_schemas.py`、`personal_task_routes.py`。
- 修改 `channel/web/app.py:create_app` 注册路由与特定异常处理；`schemas.py:WebErrorCode`（Web 错误码）追加事务错误码。
- 新增 `tests/channel/web/test_personal_tasks.py`；沿用 create_app(host_factory=...) 注入，使用临时 SQLite 与真实业务服务，模型能力使用既有测试替身。

验收：全部路由的成功、422、404、409、存储失败信封、204 删除通过；事务请求不触发模型/会话创建；旧 Web 契约测试通过。
Commit：`feat(web): expose personal task management api`。

### 阶段四：事务页面与会话视图集成

前置：阶段三契约；spec §7。

- 修改 `frontend/src/{types.ts,api.ts,App.tsx,styles.css}`；新增 `PersonalTasks.tsx`、`PersonalTaskEditor.tsx`、`usePersonalTasks.ts`。
- 复用 Dialog（通用对话框组件）与 ApiError（API 错误）；不改会话 hook 和运行控制语义。
- 新增 `PersonalTasks.test.tsx`；在现有 `App.test.tsx` 增加导航回归。

验收：创建/修改/状态/删除可用，重复点击只发一个请求；冲突不覆盖草稿；旧列表响应不覆盖最新状态；离开事务视图不取消会话运行；界面有日期、具体时刻及应用时区说明。
Commit：`feat(frontend): add personal task workspace`。

### 阶段五：交付验收与文档收口

前置：阶段一至四完成。

- 完成 spec §9 验证矩阵，修复本次引入的回归。
- 新增 `frontend/scripts/personal-tasks-acceptance.mjs`，参照既有 browser-acceptance 脚本记录浏览器操作证据；服务重启由验收执行者操作，不在页面脚本内杀进程。
- 更新 README 功能范围和入口、`docs/docker.md` 持久数据说明；只在实现验证后标记能力已交付。
- 在本目录新增 `verification.md`，记录命令、结果、浏览器证据及未覆盖事项；本轮设计交付不创建虚假验证结果。

验收：创建→编辑→重启→完成→重新打开→取消/删除真实闭环；两个页面冲突；会话运行切换回归；数据库位于已有 data 持久目录。
Commit：`test(personal-tasks): verify persistence and workspace integration`。

## 5. 验证命令与最终门槛

Windows 执行任何 Python 测试前先运行：

```powershell
.venv\Scripts\python.exe -c "import sys; print(sys.executable)"
.venv\Scripts\python.exe -m pytest tests/personal_tasks tests/channel/web tests/runtime_v2/test_phase2_application_host.py
```

前端在 `frontend` 目录运行 `npm test`、`npm run build`。依赖安装沿用项目 editable install：`.venv\Scripts\python.exe -m pip install -e ".[dev,gui]"`；当前仓库没有 Python 锁文件，本 PR 不引入新的依赖管理体系。不调用 PATH 中的 Python。

最终检查：所有需求映射到 spec §9；业务状态只由服务决定，存储修改都经过版本检查；不依赖会话 ID；无提醒/Agent 执行占位；旧数据不变；所有注释为中文。新增测试纳入默认收集。只在新增失败、代码改变或证据缺口时扩大验证，不为本功能无条件重跑真实模型基准。

## 6. 本次文档交付边界

用户已确认产品语义及两项补充决策：允许同名、不自动去重；旧版本保存必须冲突。本文与 spec 将实现选择固定为可执行合同，不把未实现能力当作事实。目前没有必须由用户补充才能确定的产品语义问题。
