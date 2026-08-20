from __future__ import annotations

import argparse
import json
from pathlib import Path

from harnessbench.config import load_app_config
from harnessbench.grading.process_grade import compute_scoring
from harnessbench.tasks import load_tasks


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="重新评分已保存的 Harness-Bench 结果")
    parser.add_argument("results", nargs="+", type=Path, help="待重新评分的结果 JSON")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    app_config = load_app_config()
    tasks = load_tasks(app_config.tasks_dir)

    for result_path in args.results:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        task_id = str(payload["task_id"])
        if task_id not in tasks:
            raise KeyError(f"未知任务: {task_id}")

        # 只复用已保存轨迹和 Oracle 结果，避免再次执行被测代理。
        payload["scoring"] = compute_scoring(
            tasks[task_id],
            Path(payload["sandbox"]),
            payload["oracle_result"],
        )
        temporary_path = result_path.with_suffix(result_path.suffix + ".tmp")
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.replace(result_path)
        print(
            json.dumps(
                {
                    "task_id": task_id,
                    "process_score": payload["scoring"].get("process_score"),
                    "combined_score": payload["scoring"].get("combined_score"),
                    "rubric_skipped": payload["scoring"].get("rubric", {}).get("skipped"),
                },
                ensure_ascii=False,
            )
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
