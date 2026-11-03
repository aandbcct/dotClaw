"""在单个开发容器内管理前后端，任一服务退出时关闭整个进程组。"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time


def stop_processes(processes: list[subprocess.Popen]) -> None:
    """先转发终止信号，再清理超过退出期限的子进程组。"""
    for process in processes:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 20
    while any(process.poll() is None for process in processes) and time.monotonic() < deadline:
        time.sleep(0.1)
    # 即便父进程已退出，也清理仍存活的同组孙进程。
    for process in processes:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def main() -> int:
    """以独立进程组启动服务，统一处理 Docker stop 和服务失败。"""
    stopping = False

    def request_stop(signum: int, frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    processes: list[subprocess.Popen] = []
    try:
        processes.append(subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "dotclaw.channel.web.app:create_app",
             "--factory", "--host", "0.0.0.0", "--port", "8765", "--reload",
             "--reload-dir", "/app/src", "--reload-delay", "0.5"],
            cwd="/app", start_new_session=True,
        ))
        processes.append(subprocess.Popen(
            ["node", "node_modules/vite/bin/vite.js", "--host", "0.0.0.0",
             "--port", "5173", "--strictPort"],
            cwd="/app/frontend", start_new_session=True,
        ))
        while not stopping:
            for process in processes:
                result = process.poll()
                if result is not None:
                    print(f"开发服务退出，关闭容器中的全部服务：pid={process.pid}, code={result}", flush=True)
                    return result if result > 0 else 1
            time.sleep(0.2)
        return 0
    finally:
        stop_processes(processes)


if __name__ == "__main__":
    sys.exit(main())
