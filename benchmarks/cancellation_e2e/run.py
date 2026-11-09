"""准备隔离源码、运行交错浏览器样本并导出可审阅结果。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile


REVISIONS = ("479e3cb", "7860daa")
SCENARIOS = ("before_first", "streaming", "command", "delegation")


def prepare(root: Path, reports: Path):
    """通过 git archive 导出原源码，借用已安装前端构建工具。"""
    for revision in REVISIONS:
        source = reports / "sources" / revision
        if not source.exists():
            source.mkdir(parents=True)
            archive = subprocess.check_output(["git", "archive", revision], cwd=root)
            with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
                bundle.extractall(source, filter="data")
        frontend = source / "frontend"
        if not (frontend / "node_modules").exists():
            subprocess.run(["cmd", "/c", "mklink", "/J", str(frontend / "node_modules"), str(root / "frontend" / "node_modules")], check=True, capture_output=True)
        with (reports / f"build-{revision}.log").open("w", encoding="utf-8") as log:
            subprocess.run(["node", str(frontend / "node_modules/vite/bin/vite.js"), "build"], cwd=frontend, stdout=log, stderr=subprocess.STDOUT, check=True)


def summarize(samples):
    """按真实成功子集算延迟，超时和入口缺失始终保留在正式分母。"""
    result = {}
    for revision in REVISIONS:
        result[revision] = {}
        for scenario in SCENARIOS:
            selected = [item for item in samples if item["version"] == revision and item["scenario"] == scenario and item["phase"] == "formal"]
            denominator = len(selected)
            success = [item for item in selected if item.get("cancel_success")]
            stats = dict(n=denominator, cancel_success_n=len(success), overall_success_n=sum(item.get("overall_success", False) for item in selected), timeout_n=sum(item["status"] == "timeout" for item in selected), entry_unavailable_n=sum(item["status"] == "entry_unavailable" for item in selected), invalid_fixture_n=sum(item["status"] == "invalid_fixture" for item in selected), refresh_error_n=sum(item.get("refresh_error") is True for item in selected), followup_success_n=sum(item.get("followup_success", False) for item in selected), page_false_success_n=sum(item.get("page_false_success", False) for item in selected), unrelated_task_stopped_n=sum(item.get("unrelated_task_stopped") is True for item in selected), latency={})
            stats["residual_resource_n"] = sum(item.get("residual_resource") is True for item in selected)
            for key in ("cancel_success", "overall_success", "timeout"):
                stats[key + "_rate"] = stats[key + "_n"] / denominator if denominator else None
            for field in ("browser_click_to_terminal_ms", "backend_receive_to_resource_exit_ms", "backend_receive_to_terminal_commit_ms"):
                values = sorted(item[field] for item in success if item.get(field) is not None)
                def percentile(percent):
                    position = (len(values) - 1) * percent
                    lower = int(position)
                    upper = min(lower + 1, len(values) - 1)
                    return values[lower] + (values[upper] - values[lower]) * (position - lower)
                stats["latency"][field] = dict(n=len(values), p50=percentile(.5) if values else None, p95=percentile(.95) if values else None, max=max(values) if values else None)
            values = sorted(item["backend_receive_to_resource_exit_ms"] for item in selected if item.get("backend_receive_to_resource_exit_ms") is not None)
            stats["resource_exit_observed_all_statuses"] = dict(n=len(values), p50=percentile(.5) if values else None, p95=percentile(.95) if values else None, max=max(values) if values else None, note="独立局部诊断，包含取消未成功但真实观测到本地资源退出的样本，不能混入核心成功延迟")
            for threshold in (5000, 10000):
                stats[f"completed_gt_{threshold}ms_n"] = sum(item["browser_click_to_terminal_ms"] > threshold for item in success)
                stats[f"completed_gt_{threshold}ms_rate"] = stats[f"completed_gt_{threshold}ms_n"] / denominator if denominator else None
                stats[f"not_completed_by_{threshold}ms_n"] = sum(not item.get("cancel_success") or item.get("browser_click_to_terminal_ms", float("inf")) > threshold for item in selected)
                stats[f"not_completed_by_{threshold}ms_rate"] = stats[f"not_completed_by_{threshold}ms_n"] / denominator if denominator else None
            stats["completed_gt_10000ms_rate"] = None
            stats["completed_gt_10000ms_estimable"] = False
            stats["completed_gt_10000ms_note"] = "10秒截止后隔离清理，真实迟于10秒才完成的比例不可估；n仅是截止前的观测计数"
            result[revision][scenario] = stats
    return result


def export(reports: Path, destination: Path, config):
    """小型原始样本进入基线目录，大型调试材料保留在忽略目录。"""
    destination.mkdir(parents=True, exist_ok=True)
    prior_path = Path(config.get("prior_invalid_run", ""))
    if prior_path.is_file():
        prior_root = prior_path.parent
        prior_marker = json.loads(prior_path.read_text(encoding="utf-8"))
        prior_marker["raw_source_directory"] = str(prior_root.resolve())
        prior_marker["excluded_from_current_formal_statistics"] = True
        (destination / "invalid-run-v1.json").write_text(json.dumps(prior_marker, ensure_ascii=False, indent=2), encoding="utf-8")
        raw_copy_root = destination / "invalid-run-v1-raw"
        raw_copy_root.mkdir(exist_ok=True)
        originals = [prior_path, *sorted(prior_root.glob("*/samples.jsonl"))]
        for original in originals:
            copied = raw_copy_root / (original.name if original == prior_path else original.parent.name + ".samples.jsonl")
            if not copied.exists():
                shutil.copyfile(original, copied)
        for phase in ("debug", "warmup", "formal"):
            prior_samples = []
            for path in sorted(prior_root.glob("*/samples.jsonl")):
                prior_samples.extend(item for item in path.read_text(encoding="utf-8").splitlines() if json.loads(item)["phase"] == phase)
            (destination / f"invalid-run-v1.{phase}.jsonl").write_text("\n".join(prior_samples) + ("\n" if prior_samples else ""), encoding="utf-8")
    samples = []
    for path in sorted(reports.glob("*/samples.jsonl")):
        samples.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    for phase in ("debug", "warmup", "formal"):
        selected = [item for item in samples if item["phase"] == phase]
        (destination / f"{phase}.jsonl").write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in selected), encoding="utf-8")
    summary = summarize(samples)
    formal = [item for item in samples if item["phase"] == "formal"]
    warmup = [item for item in samples if item["phase"] == "warmup"]
    complete = all(sum(item["version"] == revision and item["scenario"] == scenario and item["round"] == round_index for item in formal) == 10 for revision in REVISIONS for scenario in SCENARIOS for round_index in (1, 2)) and all(sum(item["version"] == revision and item["scenario"] == scenario for item in warmup) == 2 for revision in REVISIONS for scenario in SCENARIOS)
    qualification = dict(measurement_qualified=complete and not any(item["status"] == "invalid_fixture" for item in formal), complete=complete, formal_count=len(formal), expected_formal_count=160, warmup_count=len(warmup), expected_warmup_count=16, formal_invalid_fixture_count=sum(item["status"] == "invalid_fixture" for item in formal), note="产品失败、超时和入口缺失不等于测量无效；它们仍进入正式分母")
    (destination / "qualification.json").write_text(json.dumps(qualification, ensure_ascii=False, indent=2), encoding="utf-8")
    successful = [item for item in samples if item["phase"] == "formal" and item.get("cancel_success")]
    boundary_suspects = [item for item in successful if any(item.get(field, 0) is not None and item.get(field, 0) >= 9000 for field in ("browser_click_to_terminal_ms", "backend_receive_to_resource_exit_ms", "backend_receive_to_terminal_commit_ms"))]
    boundary_audit = dict(success_n=len(successful), browser_max_ms=max((item["browser_click_to_terminal_ms"] for item in successful), default=None), resource_before_or_at_commit_n=sum(item.get("backend_resource_exit_s") is not None and bool(item.get("backend_terminal_commits_s")) and item["backend_resource_exit_s"] <= max(item["backend_terminal_commits_s"].values()) for item in successful), near_9000ms_sample_ids=[{key: item[key] for key in ("version", "scenario", "round", "attempt")} for item in boundary_suspects], observer_start="Playwright button.click return", dom_deadline_start="native DOM click event", unrelated_check="at cancellation core observation end before refresh/followup")
    (destination / "boundary-audit.json").write_text(json.dumps(boundary_audit, ensure_ascii=False, indent=2), encoding="utf-8")
    (destination / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (destination / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    environment = {}
    for path in reports.glob("*/environment.json"):
        environment[path.parent.name] = json.loads(path.read_text(encoding="utf-8"))
    dependency_path = reports / "dependency-versions.json"
    if dependency_path.exists():
        environment["dependencies"] = json.loads(dependency_path.read_text(encoding="utf-8"))
    environment["source_provenance"] = {
        revision: dict(commit=subprocess.check_output(["git", "rev-parse", revision], text=True).strip(), source=str(reports / "sources" / revision), frontend_assets=sorted(path.name for path in (reports / "sources" / revision / "frontend/dist/assets").glob("index-*")))
        for revision in REVISIONS
    }
    (destination / "environment.json").write_text(json.dumps(environment, ensure_ascii=False, indent=2), encoding="utf-8")
    artifact_files = [path for directory in reports.iterdir() if directory.is_dir() and directory.name != "sources" for path in directory.rglob("*") if path.is_file()]
    artifact_inventory = dict(path=str(reports), measurement_artifact_file_count=len(artifact_files), measurement_artifact_bytes=sum(path.stat().st_size for path in artifact_files), excluded_source_exports=str(reports / "sources"), source_exports_note="原版源码、前端构建输出及指向现有node_modules的联接，不加入Git基线目录")
    (destination / "artifact-inventory.json").write_text(json.dumps(artifact_inventory, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 浏览器取消端到端基准", "", "取消核心成功要求：生产停止按钮真实点击、权威取消终态真实落盘、DOM 显示已停止、归属本地资源实际退出。整体成功另要求刷新一致、同会话继续成功、无误停独立会话。", "", "| 版本 | 场景 | 正式 n | 取消成功 | 整体成功 | 超时 | 入口缺失 | 成功延迟 n | P50 ms | P95 ms | max ms |", "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for revision in REVISIONS:
        for scenario, stats in summary[revision].items():
            latency = stats["latency"]["browser_click_to_terminal_ms"]
            def number(value):
                return "null" if value is None else f"{value:.2f}"
            def rate(name):
                value = stats[name + "_rate"]
                return "null" if value is None else f"{stats[name + '_n']}/{stats['n']} ({value:.0%})"
            lines.append(f"| {revision} | {scenario} | {stats['n']} | {rate('cancel_success')} | {rate('overall_success')} | {rate('timeout')} | {stats['entry_unavailable_n']} | {latency['n']} | {number(latency['p50'])} | {number(latency['p95'])} | {number(latency['max'])} |")
    lines += ["", "| 版本 | 场景 | 刷新未回显取消 | 同会话跟进成功 | 页面假成功 | 核心观测结束时资源残留 | 核心观测结束时误停独立任务 |", "|---|---|---:|---:|---:|---:|---:|"]
    for revision in REVISIONS:
        for scenario, stats in summary[revision].items():
            lines.append(f"| {revision} | {scenario} | {stats['refresh_error_n']} | {stats['followup_success_n']} | {stats['page_false_success_n']} | {stats['residual_resource_n']} | {stats['unrelated_task_stopped_n']} |")
    lines += ["", "未能点击停止的入口缺失样本没有取消后的残留资源观测，raw 中为 null；表内残留数只数明确观测为真的记录，不把未知当作资源已退出。"]
    lines += ["", "| 版本 | 场景 | >5s 完成/全部正式 | >10s 完成/全部正式 | 5s 截止未完成数 | 10s 截止未完成数 |", "|---|---|---:|---:|---:|---:|"]
    for revision in REVISIONS:
        for scenario, stats in summary[revision].items():
            lines.append(f"| {revision} | {scenario} | {stats['completed_gt_5000ms_n']}/{stats['n']} | 不可估（右删失） | {stats['not_completed_by_5000ms_n']} | {stats['not_completed_by_10000ms_n']}/{stats['n']} |")
    lines += ["", ">5s 完成仅统计截止前真实观测到完成者占全部正式分母，须结合成功延迟 n。10s 截止后测试隔离清理，无法知道之后何时自然完成，真实 >10s 完成比例为 null/不可估。10s 内未完成包含超时、入口缺失与其他失败，首表分别列出。每场景二十条有效样本的 P95 仅为探索性的样本分位；旧版无成功时延迟 n=0，各分位为 null，不能将十秒删失值当 P50。", "", "| 版本 | 场景 | 后端阶段 | 成功 n | P50 ms | P95 ms | max ms |", "|---|---|---|---:|---:|---:|---:|"]
    for revision in REVISIONS:
        for scenario, stats in summary[revision].items():
            for field, label in (("backend_receive_to_resource_exit_ms", "接收到本地资源退出"), ("backend_receive_to_terminal_commit_ms", "接收到终态提交完成")):
                latency = stats["latency"][field]
                lines.append(f"| {revision} | {scenario} | {label} | {latency['n']} | {number(latency['p50'])} | {number(latency['p95'])} | {number(latency['max'])} |")
    lines += ["", "局部诊断另列所有真实观测到的资源退出，包含取消未成功的样本；不能与上表成功取消子集混列。旧版本地模型调用可能更早退出，但缺少权威终态；新版这段局部耗时增加也保留原值，不用整体功能收益掩盖。该差异只涉及固定本地驱动路径，不能外推真实供应商。", "", "| 版本 | 场景 | 已观测资源退出 n | P50 ms | P95 ms | max ms |", "|---|---|---:|---:|---:|---:|"]
    for revision in REVISIONS:
        for scenario, stats in summary[revision].items():
            latency = stats["resource_exit_observed_all_statuses"]
            lines.append(f"| {revision} | {scenario} | {latency['n']} | {number(latency['p50'])} | {number(latency['p95'])} | {number(latency['max'])} |")
    lines += ["", f"边界审计：正式核心成功 {len(successful)} 条；浏览器最大 {number(boundary_audit['browser_max_ms'])}ms；本地资源退出不晚于后端终态提交 {boundary_audit['resource_before_or_at_commit_n']} 条；任一观测指标达到 9 秒的存疑样本 {len(boundary_suspects)} 条。详情见 boundary-audit.json。Python 十秒循环从 Playwright 点击返回后起算，DOM 十秒限制从真实点击事件起算，资源与提交窗口可能多出点击返回等待；没有跨钟映射，不能笼统声称五个时刻严格共享同一十秒截止。", "", "独立会话无误停仅检查到取消核心观测结束时，未覆盖后续刷新/跟进全过程。"]
    if prior_path.is_file():
        lines += ["", "初次整批运行因测试初始化竞态被判无效：原页面读取会话列表后的自动打开可能覆盖过早点击的新对话，导致部分样本未进入原目标屏障。全部旧批次单列 invalid-run-v1.*，没有挑选其中可用样本进入本次分母。原 raw 目录完整保留；中断样本标记 run_aborted，未伪装正常完成。本轮协议等待列表响应体与页面初始化完成，核对浏览器选中会话、目标运行输入、取消请求归属及该目标真实落盘终态后重新预热与交错正式。调试、废弃批次和从头重跑增加了执行时间，属于测量修正，没有生产代码改动。"]
    lines += ["", "超时为 10 秒右删失，不能理解成耗时 10 秒的成功。无入口样本保留正式分母，延迟为 null。debug 与预热单独保存，不合并正式指标。", "", "本地 OpenAI 兼容 HTTP/SSE 服务只固定模型输出与等待输入；保留每版真实驱动、模型代理、适配器、运行时、工具、委派、文件仓储和 React 界面。资源退出为本地驱动 finally 或命令父子 PID 死亡；不据此声称线上远端任务退出。", "", "浏览器总耗时使用同一 performance.now 时钟；后端分段使用同一 perf_counter 时钟。未建立跨钟映射，terminal_to_page_ms 始终 null。PID 死亡使用 10ms 周期只读轮询，观测时刻包含轮询上界与系统调度延迟。", "", "刷新后取消记录消失、旧版无权威终态、委派挂起后停止入口缺失都保留为产品结果，不修改原实现。", "", f"大型截图、源码导出、前端 dist、数据目录和执行日志：`{reports}`。测量工件 {len(artifact_files)} 个文件，共 {artifact_inventory['measurement_artifact_bytes']} 字节（不含源码导出及 node_modules 联接）。只提交本目录小型 JSON/JSONL 与报告；没有真实供应商密钥或业务正文。", "", "这次结果只支持固定本地输入、Windows 与缓存 Chromium 的版本比较，不是线上真实供应商延迟或生产可靠性承诺。", ""]
    (destination / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reports", type=Path, default=Path("reports/cancellation_e2e_20261109_retry_v2"))
    parser.add_argument("--run-id", default="20261109-browser-v2")
    parser.add_argument("--browser", required=True)
    parser.add_argument("--export-only", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    reports = args.reports.resolve()
    reports.mkdir(parents=True, exist_ok=True)
    destination = root / "benchmarks/baselines/cancellation_e2e_v1" / args.run_id
    config = dict(schema_version=2, protocol_version=2, prior_invalid_run="reports/cancellation_e2e_20261109/invalid-run-v1.json", old_revision=REVISIONS[0], new_revision=REVISIONS[1], scenarios=SCENARIOS, deadline_ms=10000, operation_seconds=60, warmup_per_version_scenario=2, formal_per_version_scenario=20, rounds=2, formal_per_round=10, version_order=[list(REVISIONS), list(reversed(REVISIONS))], browser=args.browser, clock_mapping=None, frozen_at=datetime.now(timezone.utc).isoformat(), command_interpreter=sys.executable, browser_dependency="playwright==1.57.0")
    frozen_path = reports / "frozen-config.json"
    if frozen_path.exists():
        config = json.loads(frozen_path.read_text(encoding="utf-8"))
    elif not args.export_only:
        frozen_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    if not args.export_only:
        prepare(root, reports)
        batches = [("warmup", 0, revision, 2) for revision in REVISIONS]
        batches += [("formal", round_index, revision, 10) for round_index, order in enumerate((REVISIONS, tuple(reversed(REVISIONS))), 1) for revision in order]
        for phase, round_index, revision, count in batches:
            output = reports / f"{phase}-round{round_index}-{revision}"
            output.mkdir(exist_ok=True)
            if (output / "samples.jsonl").exists():
                raise RuntimeError(f"拒绝覆盖已有样本：{output}")
            command = [sys.executable, str(root / "benchmarks/cancellation_e2e/worker.py"), "--source", str(reports / "sources" / revision), "--commit", revision, "--output", str(output), "--browser", args.browser, "--phase", phase, "--round", str(round_index), "--count", str(count)]
            (output / "command.json").write_text(json.dumps(command, ensure_ascii=False), encoding="utf-8")
            print(f"Starting {phase} round={round_index} version={revision}", flush=True)
            with (output / "execution.log").open("w", encoding="utf-8") as log:
                subprocess.run(command, cwd=root, stdout=log, stderr=subprocess.STDOUT, check=True)
            export(reports, destination, config)
    export(reports, destination, config)
    print(str(destination), flush=True)


if __name__ == "__main__":
    main()
