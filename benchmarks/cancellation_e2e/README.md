# 浏览器取消端到端基准

在 Windows 的项目 `.venv` 内执行。不会读取项目真实供应商配置，不使用现有 Docker 实例，不修改任何生产代码。每个版本由 `git archive` 导出完整源码，分别构建该版本 React/Vite 界面，使用该版本原 Web 工厂提供真实 HTTP 服务。

```powershell
.venv\Scripts\python.exe -c "import sys; print(sys.executable)"
.venv\Scripts\python.exe -m pip install -r benchmarks/cancellation_e2e/requirements.txt
.venv\Scripts\python.exe -m pytest tests/benchmarks/test_cancellation_e2e_contract.py -q
.venv\Scripts\python.exe benchmarks/cancellation_e2e/run.py --browser "$env:LOCALAPPDATA/ms-playwright/chromium_headless_shell-1200/chrome-headless-shell-win64/chrome-headless-shell.exe"
```

本次使用已存在的 Chromium 1200 缓存，无下载浏览器；Playwright 精确版本 `1.57.0`。复现环境需先安装项目现有 Python 依赖及 `frontend/node_modules`，前端依赖不由本基准修改。构建借用工作区现有 `node_modules` 的目录联接，两个版本各自产生独立 `dist`。

`run.py` 默认执行旧版 `479e3cb`、新版 `7860daa`，各版本四场景各两次预热、二十次正式样本。两轮正式批次分别按旧→新、新→旧排列，每轮每场景十条；同版本同批次顺序固定为首包前、流式中、真实长命令进程树、父子委派。模型与命令都等待 60 秒，先观察真实进入屏障，再点击原页面停止按钮，取消成功期限 10 秒。正式运行配置写入 `reports/cancellation_e2e_20261109_retry_v2/frozen-config.json`；已有正式输出会拒绝覆盖。

初次运行 `reports/cancellation_e2e_20261109/` 整批无效，原始字节保留并由 `invalid-run-v1.json` 标记。发现测试点击“新对话”可能早于原界面异步会话列表读取/自动打开完成，后者会覆盖选中状态，导致少数正式样本没有进入目标等待屏障。该批次全部不参与重跑统计，没有挑选或补齐其中成功样本。协议二等待真实会话列表响应体与原界面 loading 结束后再创建新会话，并核对浏览器选中 ID、后端原目标输入、请求所属会话和目标运行落盘终态。旧版取消 HTTP 只携带会话，新版另携带运行 ID；旧版仍核对冻结目标运行的真实提交，不人为改造其请求协议。修正只涉及测试装配和观察，调试与重跑增加了执行时间。

`worker.py` 也可单独用于 `--phase debug --count 1`。所有调试保留在独立目录，导出为 `debug.jsonl`，不会进入正式统计。部分早期调试记录了路由/定位器错误并标 `invalid_fixture`；它们保留原样，不重写成产品结论。

被替代的边界只有本地 OpenAI 兼容供应商输出。仍保留每版原模型驱动、模型代理、适配器、运行时、上下文、策略、工具安全链、审批判断、委派、会话交互与文件仓储。基准手工给原宿主填入这些原版组件，以避免全局配置、MCP、记忆服务和真实供应商启动；这不是完整部署启动测试。命令通过生产内置工具启动项目解释器和长任务脚本，父子 PID 真实存在后才允许点击；进程只写本样本隔离目录。

包装只调用原方法并记录时刻，不实现取消/关闭/终态。纯 ASGI 接收观察不新增业务任务，避免改变旧版任务所有权。浏览器没有 API 路由拦截或假计时器。独立浏览器会话同时执行另一个 60 秒模型任务，用于检查误停。样本先冻结、写入 JSONL，随后才取消仍存活的本样本模型任务、终止已记录的归属 PID。

取消核心成功、整体成功、10 秒超时、停止入口缺失、刷新不一致、跟进失败、页面假成功、残留资源和独立任务误停分别记录。独立任务误停在取消核心结束时采样，未持续监控后续刷新与跟进全过程。委派进入等待后原界面没有停止按钮时，记录 `entry_unavailable`，保留正式分母且延迟为空，不通过 HTTP 补点。刷新检查要求取消记录在页面可见；如果原产品只投影成功会话，该检查仍失败并在报告解释边界，不隐藏结果。

浏览器点击到 DOM 终态使用同一 `performance.now()`；后端请求接收、驱动 finally/父子 PID 死亡、原取消提交返回使用同一 `time.perf_counter()`。Python 观测循环的十秒从 Playwright 点击返回后起算，DOM 成功另限制真实点击至终态不超过十秒；资源/提交边界可能包含点击返回的额外等待。报告必须核对所有成功样本是否接近边界，不能笼统声称五点都共享完全相同的十秒窗口。驱动 finally 只证明本地调用退出。远端处理器退出单独记录，不能推导真实供应商任务已停止。未建立跨钟映射，`terminal_to_page_ms` 永远为空。10 秒超时属于右删失，不能作为十秒成功进入分位数；统计另列成功样本 n。

小型原始 JSONL、配置、摘要和报告导出到 `benchmarks/baselines/cancellation_e2e_v1/<run-id>/`。源码导出、数据、构建输出、截图、命令和执行日志位于被忽略的 `reports/cancellation_e2e_20261109/`。使用 `--export-only` 可从现有原始记录重新生成统计，无需重新执行浏览器样本。
