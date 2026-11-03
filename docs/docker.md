# Docker 开发与部署

两种模式都只运行一个 dotClaw 容器：开发版运行 Vite 与自动重载的 Python 后端；部署版只运行后端，由后端提供构建后的前端文件。两份 Compose 文件独立使用，不叠加。

需要 Docker Desktop 的 Linux 容器模式和支持可选 `env_file` 的 Docker Compose 2.24+。在仓库根目录执行下文命令。

## 本地开发

```powershell
docker compose -f compose.dev.yml up --build -d
docker compose -f compose.dev.yml logs -f
```

打开 <http://localhost:5173>。Vite 在容器内将 `/api` 转发到同容器的后端 8765 端口，包括 SSE 流式响应。后端不单独映射宿主端口。

- `src`、`frontend` 绑定到本地源码，修改 Python 文件会触发 Uvicorn 重载，修改前端触发 Vite HMR。Windows Docker Desktop 使用轮询检测修改。
- Linux `node_modules` 放在独立命名卷，不使用或覆盖 Windows 的 `frontend/node_modules`。
- Python 虚拟环境在镜像的 `/opt/venv`，不挂载宿主机 `.venv`。
- `data` 与 `workspace` 绑定到仓库本地同名目录，容器写入的会话、记忆和实验文件可直接在本机查看；它们已加入 Git 忽略规则。
- Vite 或 Uvicorn 重载管理进程退出时，会关闭另一个服务并让容器以失败状态退出；`stop` 会向服务进程组转发终止信号。热重载时的应用语法错误可能只使健康检查失败，修复源码后重载管理进程会再次启动应用。

```powershell
# 停止与再次启动，无需重建镜像。
docker compose -f compose.dev.yml stop
docker compose -f compose.dev.yml start

# 删除容器和网络，保留依赖卷和本地会话、实验文件。
docker compose -f compose.dev.yml down
```

`start` 只启动已经存在的容器；执行 `down` 后改用 `up -d`。

## 模型、配置与数据

默认挂载 `docker/config.example.yaml` 与 `docker/model_router_config.example.yaml`。默认模型地址是不可用的本机占位地址：页面和会话 API 可以使用，真实对话需要先配置模型。

```powershell
Copy-Item docker/config.example.yaml docker/config.local.yaml
Copy-Item docker/model_router_config.example.yaml docker/model_router_config.local.yaml
```

修改复制后的模型路由配置，填写提供商 `base_url`、`model_id`，将 `api_key` 写为 `${OPENAI_API_KEY}` 等环境变量引用。可以参考根目录的 `model_router_config.example.yaml` 配置多个模型。密钥填写到仓库根目录 `.env`（不存在时从 `.env.example` 复制），Compose 在运行时注入，不会打入镜像。

在 `.env` 增加下面两行选择容器专用配置：

```dotenv
DOTCLAW_CONFIG=./docker/config.local.yaml
DOTCLAW_ROUTER_CONFIG=./docker/model_router_config.local.yaml
```

配置、环境变量改变后执行 `docker compose -f compose.dev.yml up -d --force-recreate`。源码热重载不负责应用 YAML 配置或依赖变更。已有根目录私人配置不会自动用于容器；确认其中路径适用于 Linux 后，也可以用上述变量显式选择它们。

例如保留容器专用主配置，复用已有模型路由文件：

```powershell
$env:DOTCLAW_CONFIG = './docker/config.local.yaml'
$env:DOTCLAW_ROUTER_CONFIG = './model_router_config.yaml'
docker compose -f compose.dev.yml up -d --force-recreate
```

| 容器路径 | 来源与用途 |
| --- | --- |
| `/app/config.yaml`、`/app/model_router_config.yaml` | 宿主配置文件，只读挂载 |
| `/app/.dotclaw/agentConfig` | 仓库 Agent 配置，只读挂载 |
| `/app/skills` | 本地 `skills` 目录，只读挂载 |
| `/app/data` | 开发版为本地 `./data`，部署版为命名卷；保存会话、运行事实、记忆和界面设置 |
| `/workspace` | 开发版为本地 `./workspace`，部署版为命名卷；默认工具读写范围 |

开发项目默认名为 `dotclaw-dev`，部署项目默认名为 `dotclaw`。部署版继续使用项目隔离的命名卷；开发版会直接读写原有宿主 `data`，避免同时运行另一个使用相同目录的本地后端。

开发版多组隔离实验必须同时选择不同的数据和工作目录、项目名及端口；只改变 `-p` 不会隔离绑定目录。例如在独立 PowerShell 窗口执行：

```powershell
$env:DOTCLAW_DATA_DIR = './tmp/experiment-a/data'
$env:DOTCLAW_WORKSPACE_DIR = './tmp/experiment-a/workspace'
$env:DOTCLAW_DEV_PORT = '15173'
docker compose -f compose.dev.yml -p dotclaw-experiment-a up -d
```

也可将 `DOTCLAW_DATA_DIR` 与 `DOTCLAW_WORKSPACE_DIR` 写入 `.env` 持久选择目录；这些变量仅影响开发版。不要为了更新依赖运行 `down -v`，它会删除依赖卷，部署版还会删除会话和工作目录卷。

从旧开发命名卷迁移时，先停止所有写入同一数据目录的后端，完整备份本地目录及旧卷（包含 SQLite 的 WAL/SHM），再迁入本地不存在的文件或完整会话目录。同名冲突保留双方完整副本并人工判断，不能直接拼接 SQLite 数据库或 WAL；验证成功前保留原卷。仅切换 Compose 挂载不会自动搬迁旧卷数据。

容器内执行的是 Linux 命令，不能直接使用 `D:\...` 路径或宿主 PowerShell。访问宿主机模型服务时使用 `host.docker.internal`，不能把 `localhost` 当成宿主机。需要 MCP、额外命令或系统库时，应按实际配置补充镜像依赖；当前部署镜像不包含 Node.js。

## 依赖变化

修改 Python 依赖或 Dockerfile 后重建开发镜像：

```powershell
docker compose -f compose.dev.yml up --build -d
```

修改前端 `package.json` / `package-lock.json` 后更新已有依赖卷，再启动。仅重建镜像不会替换已有命名卷里的依赖：

```powershell
docker compose -f compose.dev.yml stop
docker compose -f compose.dev.yml run --rm --no-deps --workdir /app/frontend dotclaw npm ci
docker compose -f compose.dev.yml up --build -d
```

前端遵循仓库的锁文件；Python 遵循 `pyproject.toml` 的版本约束，尚未提供传递依赖锁定，重新构建不保证获得完全相同的依赖版本。需要复用完全相同的实验环境时保留并复用已构建镜像。

## 部署版

```powershell
docker compose -f compose.yml up --build -d
docker compose -f compose.yml logs -f
docker compose -f compose.yml stop
docker compose -f compose.yml start
```

打开 <http://localhost:8765>。部署版把代码与 `frontend/dist` 构建进镜像，不挂载源码、不自动重载；代码更新后重新执行 `up --build -d`。单独构建时 `docker build -t dotclaw:deployment .` 默认也是部署目标。

两版默认仅绑定宿主机 `127.0.0.1`。端口冲突时，在 `.env` 设置 `DOTCLAW_DEV_PORT=15173` 或 `DOTCLAW_PORT=18765` 后重新执行 `up -d`。可用 `docker compose -f compose.dev.yml ps` 查看健康状态。该入口仍按本地单用户使用设计。
