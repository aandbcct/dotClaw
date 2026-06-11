"""AgentTracer 单元测试（P13）"""

import json
import tempfile
from pathlib import Path

import pytest

from dotclaw.agent.tracer import AgentTracer
from dotclaw.config.settings import DebugConfig


def _cfg(enabled: bool = True) -> DebugConfig:
    return DebugConfig(enable_tracer=enabled)


class TestDisabled:
    """enable_tracer=False 时所有方法 no-op。"""

    def test_all_methods_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = AgentTracer(_cfg(enabled=False), data_root=tmp)

            t.start_session("req", "hello")
            t.start_loop(0)
            t.prompt_built([], 0, 0)
            sid = t.llm_call_start("gpt")
            t.llm_call_done(sid, success=True, duration_ms=100)
            sid2 = t.llm_response_start()
            t.llm_response_done(sid2, success=True, finish_reason="stop")
            sid3 = t.tool_exec_start("read_file", {})
            t.tool_exec_done(sid3, success=True, tool_name="read_file", result="ok")
            t.end_loop()
            t.end_session(success=True, final_response="bye")
            path = t.build_report()

            # 无文件产生
            traces_dir = Path(tmp) / "traces"
            assert not traces_dir.exists() or not list(traces_dir.rglob("*.jsonl"))
            assert path == ""


class TestTraceJSONL:
    """trace.jsonl 增量写入测试。"""

    def test_basic_session_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = AgentTracer(_cfg(), data_root=tmp)
            t.start_session("req-01", "帮我读文件")
            t.end_session(success=True, final_response="文件内容是...")

            trace_file = self._find_trace(tmp, "req-01")
            with open(trace_file, encoding="utf-8") as f:
                lines = [json.loads(l) for l in f if l.strip()]

            assert len(lines) == 2
            assert lines[0]["step"] == "session"
            assert lines[0]["state"] == "start"
            assert lines[1]["step"] == "session"
            assert lines[1]["state"] == "success"

    def test_single_round_no_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = AgentTracer(_cfg(), data_root=tmp)
            t.start_session("req-02", "hello")
            t.start_loop(0)
            t.prompt_built([], msg_count=3, est_tokens=500)

            sid_call = t.llm_call_start(model="deepseek")
            t.llm_call_done(sid_call, success=True, duration_ms=800)
            sid_resp = t.llm_response_start()
            t.llm_response_done(sid_resp, success=True,
                finish_reason="stop", duration_ms=500)

            t.end_loop()
            t.end_session(success=True, final_response="world")
            t.build_report()

            trace_file = self._find_trace(tmp, "req-02")
            with open(trace_file) as f:
                events = [json.loads(l) for l in f if l.strip()]

            steps = [(e["step"], e["state"]) for e in events]
            assert ("session", "start") in steps
            assert ("loop", "start") in steps
            assert ("prompt_built", "success") in steps
            assert ("llm_call", "start") in steps
            assert ("llm_call", "success") in steps
            assert ("llm_response", "start") in steps
            assert ("llm_response", "success") in steps
            assert ("loop", "success") in steps
            assert ("session", "success") in steps

    def test_multi_round_with_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = AgentTracer(_cfg(), data_root=tmp)
            t.start_session("req-03", "read then write")

            # Round 0 — tool calls
            t.start_loop(0)
            t.prompt_built([], msg_count=3, est_tokens=500)
            sid_call = t.llm_call_start(model="deepseek")
            t.llm_call_done(sid_call, success=True, duration_ms=800)
            sid_resp = t.llm_response_start()
            t.llm_response_done(sid_resp, success=True,
                finish_reason="tool_calls", duration_ms=400)
            sid_t1 = t.tool_exec_start("read_file", {"path": "a.txt"})
            t.tool_exec_done(sid_t1, success=True,
                tool_name="read_file", result="content A", duration_ms=150)
            t.end_loop()

            # Round 1 — final answer
            t.start_loop(1)
            t.prompt_built([], msg_count=5, est_tokens=800)
            sid_call2 = t.llm_call_start(model="deepseek")
            t.llm_call_done(sid_call2, success=True, duration_ms=600)
            sid_resp2 = t.llm_response_start()
            t.llm_response_done(sid_resp2, success=True,
                finish_reason="stop", duration_ms=300)
            t.end_loop()

            t.end_session(success=True, final_response="done")
            t.build_report()

            trace_file = self._find_trace(tmp, "req-03")
            with open(trace_file) as f:
                events = [json.loads(l) for l in f if l.strip()]

            # 统计各步骤
            llm_calls = [e for e in events if e["step"] == "llm_call"]
            tool_execs = [e for e in events if e["step"] == "tool_exec"]
            assert len(llm_calls) == 4  # 2 rounds × 2 (start + done)
            assert len(tool_execs) == 2  # 1 tool × 2

    def test_step_id_continuity(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = AgentTracer(_cfg(), data_root=tmp)
            t.start_session("req-04", "test")

            s1 = t.llm_call_start(model="gpt")
            s2 = t.tool_exec_start("ls", {})
            s3 = t.llm_call_start(model="qwen")

            assert s1 == "s_000"
            assert s2 == "s_001"
            assert s3 == "s_002"
            # After end_session, counter resets
            t.end_session(success=True)
            t.start_session("req-05", "new")
            s4 = t.llm_call_start(model="gpt")
            assert s4 == "s_000"

    def test_failure_records_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = AgentTracer(_cfg(), data_root=tmp)
            t.start_session("req-06", "fail")
            t.start_loop(0)
            t.prompt_built([], 1, 100)

            sid_call = t.llm_call_start(model="bad-model")
            t.llm_call_done(sid_call, success=False,
                error="Connection timeout")
            t.end_loop()
            t.end_session(success=False, error="Connection timeout")
            t.build_report()

            trace_file = self._find_trace(tmp, "req-06")
            with open(trace_file) as f:
                events = [json.loads(l) for l in f if l.strip()]

            llm_fail = [e for e in events
                        if e["step"] == "llm_call" and e["state"] == "failure"]
            assert len(llm_fail) == 1
            assert llm_fail[0]["error"] == "Connection timeout"

            sess_fail = [e for e in events
                         if e["step"] == "session" and e["state"] == "failure"]
            assert len(sess_fail) == 1

    # ---- helpers ----

    @staticmethod
    def _find_trace(tmp: str, req_id: str) -> str:
        """查找 req_id 对应的 trace.jsonl"""
        traces = list(Path(tmp).rglob(f"**/traces/*/{req_id}/trace.jsonl"))
        assert traces, f"trace.jsonl not found for {req_id}"
        return str(traces[0])


class TestReportJSON:
    """report.json 构建测试。"""

    def test_basic_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = AgentTracer(_cfg(), data_root=tmp)
            t.start_session("req-r1", "test")
            t.start_loop(0)
            t.prompt_built([], 3, 500)

            sid_call = t.llm_call_start(model="deepseek")
            t.llm_call_done(sid_call, success=True, duration_ms=800)
            sid_resp = t.llm_response_start()
            t.llm_response_done(sid_resp, success=True,
                finish_reason="stop", duration_ms=500)

            t.end_loop()
            t.end_session(success=True, final_response="done")
            path = t.build_report()

            assert path.endswith("report.json")
            with open(path) as f:
                report = json.load(f)

            assert report["req_id"] == "req-r1"
            assert report["state"] == "success"
            assert len(report["rounds"]) == 1
            rd = report["rounds"][0]
            assert rd["llm_call"]["state"] == "success"
            assert rd["llm_call"]["duration_ms"] == 800
            assert rd["llm_response"]["finish_reason"] == "stop"

    def test_report_with_tool_execs(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = AgentTracer(_cfg(), data_root=tmp)
            t.start_session("req-r2", "tools")
            t.start_loop(0)
            t.prompt_built([], 3, 500)

            sid_call = t.llm_call_start(model="qwen")
            t.llm_call_done(sid_call, success=True, duration_ms=500)
            sid_resp = t.llm_response_start()
            t.llm_response_done(sid_resp, success=True,
                finish_reason="tool_calls")

            sid_t1 = t.tool_exec_start("read_file", {"path": "a.txt"})
            t.tool_exec_done(sid_t1, success=True,
                tool_name="read_file", result="AAA", duration_ms=100)

            sid_t2 = t.tool_exec_start("write_file", {"path": "b.txt"})
            t.tool_exec_done(sid_t2, success=False,
                tool_name="write_file", error="Permission denied", duration_ms=50)

            t.end_loop()
            t.end_session(success=True, final_response="done")
            path = t.build_report()

            with open(path) as f:
                report = json.load(f)

            rd = report["rounds"][0]
            assert len(rd["tool_execs"]) == 2
            assert rd["tool_execs"][0]["tool_name"] == "read_file"
            assert rd["tool_execs"][0]["state"] == "success"
            assert rd["tool_execs"][1]["tool_name"] == "write_file"
            assert rd["tool_execs"][1]["state"] == "failure"

    def test_incomplete_due_to_crash(self):
        """start 了但没 done → report 中标记 incomplete"""
        with tempfile.TemporaryDirectory() as tmp:
            t = AgentTracer(_cfg(), data_root=tmp)
            t.start_session("req-r3", "crash")
            t.start_loop(0)
            t.prompt_built([], 3, 500)

            # 只 start，不 done — 模拟崩溃
            t.llm_call_start(model="deepseek")
            # 不调 t.llm_call_done

            # 强制 build_report 而不调 end_session
            # end_session 需要调，否则 report 里 session 没有终态
            t.end_session(success=False, error="crashed")
            path = t.build_report()

            with open(path) as f:
                report = json.load(f)

            rd = report["rounds"][0]
            assert rd["llm_call"]["state"] == "incomplete"

    def test_multi_round_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = AgentTracer(_cfg(), data_root=tmp)
            t.start_session("req-r4", "multi")

            for r in range(3):
                t.start_loop(r)
                t.prompt_built([], 1, 100)
                sid_call = t.llm_call_start(model="gpt")
                t.llm_call_done(sid_call, success=True, duration_ms=100)
                sid_resp = t.llm_response_start()
                t.llm_response_done(sid_resp, success=True, finish_reason="stop")
                t.end_loop()

            t.end_session(success=True, final_response="ok")
            path = t.build_report()

            with open(path) as f:
                report = json.load(f)

            assert len(report["rounds"]) == 3
            for rd in report["rounds"]:
                assert rd["llm_call"]["state"] == "success"

    def test_report_with_error_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = AgentTracer(_cfg(), data_root=tmp)
            t.start_session("req-r5", "ooops")
            t.start_loop(0)
            t.prompt_built([], 1, 100)
            sid = t.llm_call_start(model="bad")
            t.llm_call_done(sid, success=False, error="timeout")
            t.end_loop()
            t.end_session(success=False, error="API connection failed")
            path = t.build_report()

            with open(path) as f:
                report = json.load(f)

            assert report["state"] == "failure"
            assert "error" in report
