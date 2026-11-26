"""Unit tests for splash_monitor — no GUI, no running engine required.

Run:  python -m unittest discover -s tests -v
"""
import json
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import splash_monitor as sm  # noqa: E402


class FakeCanvas:
    """Records Tkinter-Canvas-like calls; fixed size."""

    def __init__(self, w=400, h=46):
        self._w, self._h = w, h
        self.lines, self.polys = [], []

    def winfo_width(self):
        return self._w

    def winfo_height(self):
        return self._h

    def delete(self, _tag):
        self.lines, self.polys = [], []

    def create_line(self, *coords, **kw):
        self.lines.append((list(coords), kw))

    def create_polygon(self, coords, **kw):
        self.polys.append((list(coords), kw))


class TestEndpoint(unittest.TestCase):
    def test_default(self):
        self.assertEqual(sm.make_base(), "http://127.0.0.1:8000")

    def test_port_shorthand(self):
        self.assertEqual(sm.make_base(port=9001), "http://127.0.0.1:9001")

    def test_url_wins_and_rstrips(self):
        self.assertEqual(sm.make_base(port=1, url="http://host:8080/"),
                         "http://host:8080")


class TestParseMetrics(unittest.TestCase):
    SAMPLE = "\n".join([
        "# comment",
        "splash_ttft_p50_milliseconds 2658.5",
        "splash_decode_output_tokens_total 12345",
        "splash_memory_current_bytes 46170898432",
        'splash_memory_pressure{state="normal"} 1',
        'splash_memory_pressure{state="warning"} 0',
        'splash_thermal_state{state="nominal"} 1',
        'splash_thermal_state{state="serious"} 0',
        "totally unrelated line",
        "splash_broken",
    ])

    def test_scalars(self):
        m = sm.parse_metrics(self.SAMPLE)
        self.assertEqual(m["ttft_p50_milliseconds"], 2658.5)
        self.assertEqual(m["decode_output_tokens_total"], 12345)
        self.assertEqual(m["memory_current_bytes"], 46170898432.0)

    def test_labeled_active_only(self):
        m = sm.parse_metrics(self.SAMPLE)
        self.assertEqual(m["pressure_engine"], "normal")   # 只取值为 1 的 state
        self.assertEqual(m["thermal"], "nominal")

    def test_empty_and_garbage(self):
        self.assertEqual(sm.parse_metrics(""), {})
        self.assertEqual(sm.parse_metrics("nothing here"), {})


class TestRateFromDelta(unittest.TestCase):
    K = (sm.KEY_DEC_TOKENS, sm.KEY_DEC_WALL)

    def _mk(self, t0, w0, t1, w1):
        prev = {self.K[0]: t0, self.K[1]: w0}
        met = {self.K[0]: t1, self.K[1]: w1}
        return prev, met

    def test_normal(self):
        prev, met = self._mk(100, 1000.0, 200, 3000.0)   # 100 tok / 2s wall
        rate, act = sm.rate_from_delta(prev, met, *self.K)
        self.assertAlmostEqual(rate, 50.0)
        self.assertTrue(act)

    def test_no_window_truncation(self):
        # 引擎自报墙钟 0.1s 内跑了 100 tok —— 不因采样窗口更久而被摊薄
        prev, met = self._mk(0, 0.0, 100, 100.0)
        rate, _ = sm.rate_from_delta(prev, met, *self.K)
        self.assertAlmostEqual(rate, 1000.0)

    def test_tiny_wall_clamped(self):
        prev, met = self._mk(0, 0.0, 100, 20.0)          # d_wall=0.02s -> clamp 0.05
        rate, _ = sm.rate_from_delta(prev, met, *self.K)
        self.assertAlmostEqual(rate, 2000.0)

    def test_counter_reset(self):
        prev, met = self._mk(500, 9000.0, 10, 100.0)     # 引擎重启, 计数倒退
        self.assertEqual(sm.rate_from_delta(prev, met, *self.K), (None, False))

    def test_missing_inputs(self):
        self.assertEqual(sm.rate_from_delta(None, {}, *self.K), (None, False))
        self.assertEqual(sm.rate_from_delta({}, {}, *self.K), (None, False))
        prev, met = self._mk(0, 0.0, 0, 0.0)             # 无活动
        self.assertEqual(sm.rate_from_delta(prev, met, *self.K), (None, False))


class TestFreshFmt(unittest.TestCase):
    def test_fresh(self):
        self.assertTrue(sm.fresh(time.time()))
        self.assertFalse(sm.fresh(time.time() - 10, limit=8))
        self.assertFalse(sm.fresh(None))

    def test_fmt(self):
        self.assertEqual(sm.fmt(None), "--")
        self.assertEqual(sm.fmt(1.234), "1.2")
        self.assertEqual(sm.fmt(1.234, "{:.0f}"), "1")


class TestChartGeom(unittest.TestCase):
    def test_spans_full_width(self):
        cv = FakeCanvas(400, 46)
        pts, top, x0, step, base, span = sm._chart_geom(cv, [1, 2, 3], None)
        self.assertEqual(x0, 1.0)
        self.assertAlmostEqual(x0 + 2 * step, 399.0)     # 横向铺满 1..w-1
        self.assertEqual(base, 45.0)                     # 基线贴底
        self.assertEqual(top, 3)

    def test_vmax_wins(self):
        cv = FakeCanvas(400, 46)
        _, top, *_ = sm._chart_geom(cv, [1, 2], 4000)
        self.assertEqual(top, 4000)

    def test_none_when_empty_or_tiny(self):
        self.assertIsNone(sm._chart_geom(FakeCanvas(), [], None))
        self.assertIsNone(sm._chart_geom(FakeCanvas(10, 5), [1], None))

    def test_window_is_last_90(self):
        cv = FakeCanvas()
        pts, *_ = sm._chart_geom(cv, list(range(200)), None)
        self.assertEqual(len(pts), 90)
        self.assertEqual(pts[0], 110)


class TestDraw(unittest.TestCase):
    def test_zero_sits_on_baseline(self):
        cv = FakeCanvas(400, 46)
        sm.draw_line(cv, [0, 0], "#000")
        xs, ys = cv.lines[0][0][0::2], cv.lines[0][0][1::2]
        self.assertTrue(all(y == 45.0 for y in ys))      # y = h-1
        self.assertEqual((xs[0], xs[-1]), (1.0, 399.0))

    def test_none_splits_segments(self):
        cv = FakeCanvas()
        sm.draw_line(cv, [1, None, 1], "#000")
        self.assertEqual(len(cv.lines), 2)               # 断线成两段

    def test_single_point_drawn_as_dot(self):
        cv = FakeCanvas()
        sm.draw_line(cv, [5], "#000")
        coords, kw = cv.lines[0]
        self.assertAlmostEqual(coords[2] - coords[0], 0.01)  # 极短线
        self.assertEqual(kw.get("capstyle"), "round")

    def test_clamped_to_top(self):
        cv = FakeCanvas(400, 46)
        sm.draw_line(cv, [999], "#000", vmax=100)
        y = cv.lines[0][0][1]
        self.assertAlmostEqual(y, 45.0 - 42.0)           # 钳位到 span 顶

    def test_area_closes_on_baseline(self):
        cv = FakeCanvas(400, 46)
        sm.draw_area(cv, [0.5, 0.25], "#0f0", "#cfc")
        coords, _ = cv.polys[0]
        # 闭合顺序: 折线点 ... -> (x_last, base) -> (x_first, base)
        self.assertEqual(coords[-4:], [399.0, 45.0, 1.0, 45.0])

    def test_area_none_gap_splits(self):
        cv = FakeCanvas()
        sm.draw_area(cv, [0.5, 0.4, None, 0.5, 0.6], "#0f0", "#cfc")
        self.assertEqual(len(cv.polys), 2)


class TestCollectorOffline(unittest.TestCase):
    def test_dead_port_sets_status_no_fake_data(self):
        c = sm.Collector("http://127.0.0.1:59999")
        c._tick()
        s = c.snapshot()
        self.assertIn("连不上", s["status"])
        self.assertIn("59999", s["status"])
        self.assertIsNone(s.get("mem_cur"))              # 不造假 0
        self.assertEqual(s["hist_decode"], [])           # 断线期间不画假历史

    def test_restart_detection(self):
        calls = {"n": 0}
        real = sm.http_text

        def fake(path, base, timeout=5):
            calls["n"] += 1
            tok = 500 if calls["n"] == 1 else 10         # 计数倒退 = 引擎重启
            return "splash_requests_completed_total %d\n" % tok

        sm.http_text = fake
        try:
            c = sm.Collector("http://x")
            c._tick()
            first_done = c._last_req_done
            c._tick()
            s = c.snapshot()
            self.assertEqual(first_done, 500)
            self.assertIsNotNone(s.get("restarted_at"))
            self.assertEqual(s["status"], "ok")
        finally:
            sm.http_text = real


class TestBenchmarkOffline(unittest.TestCase):
    def test_dead_port_reports_failure(self):
        got = []
        sm.run_benchmark("http://127.0.0.1:59999", lambda k, v: got.append((k, v)))
        keys = dict(got)
        self.assertTrue(str(keys.get("bench_status", "")).startswith("压测失败"))
        self.assertIn(("bench_running", False), got)     # 失败后必须释放锁


class TestBenchLock(unittest.TestCase):
    def test_double_start_is_ignored(self):
        real = sm.run_benchmark
        release = threading.Event()
        started = threading.Event()

        def stub(base, on_update):
            started.set()
            release.wait(5)
            on_update("bench_running", False)

        sm.run_benchmark = stub
        try:
            c = sm.Collector("http://x")
            self.assertTrue(c.try_start_bench())
            started.wait(5)
            self.assertFalse(c.try_start_bench())        # 竞态: 二次点击被拒
            release.set()
            time.sleep(0.2)
            self.assertTrue(c.try_start_bench())         # 结束后可再启动
            release.set()
            time.sleep(0.2)
        finally:
            sm.run_benchmark = real
            release.set()


class TestEngineStatusOffline(unittest.TestCase):
    def test_returns_none_not_crash(self):
        self.assertIsNone(sm.engine_status("http://127.0.0.1:59999"))


class TestSystemCollectors(unittest.TestCase):
    """macOS 采集器在异常输入/环境下必须返回 None 而非崩溃或造假数据。"""

    def test_gpu_stats_shape(self):
        g = sm.gpu_stats()
        self.assertEqual(set(g), {"device", "renderer", "vram_bytes", "ts"})
        if g["ts"] is None:                              # 非 macOS / 无计数器
            self.assertIsNone(g["device"])

    def test_pressure_swap_shape(self):
        p = sm.pressure_and_swap()
        self.assertIsNone(p["level"])                    # 永不推断压力等级
        self.assertTrue(p["free_pct"] is None or 0 <= p["free_pct"] <= 100)


if __name__ == "__main__":
    unittest.main()


# =====================================================================
# Regression tests for pre-release review fixes (P1-1..P1-4, P2-1..P2-4)
# =====================================================================

class TestAvgRate(unittest.TestCase):
    """P1-3: lifetime 平均 = 累计 tokens / 累计墙钟; 缺失不得当 0。"""

    def test_basic(self):
        self.assertAlmostEqual(sm.avg_rate(600, 3000), 200.0)

    def test_real_zero_kept(self):
        self.assertEqual(sm.avg_rate(0, 1000), 0.0)        # 真实 0 保留

    def test_missing_or_dead_denominator(self):
        self.assertIsNone(sm.avg_rate(None, 1000))
        self.assertIsNone(sm.avg_rate(100, None))
        self.assertIsNone(sm.avg_rate(100, 0))
        self.assertIsNone(sm.avg_rate(100, -5))


class TestCliSemantics(unittest.TestCase):
    """P1-3: CLI 的"官方均值"必须是累计值换算, ITL 换算只能作标注过的回退。"""

    def test_cli_uses_lifetime_average(self):
        import inspect
        src = inspect.getsource(sm.cli_mode)
        self.assertIn('s.get("dec_avg")', src)
        self.assertNotIn("官方均值 {fmt(1000/", src)       # 禁止再拿 1000/ITL 冒称均值
        self.assertIn("ITL p50换算", src)                  # 回退路径存在且明确标注


class TestTickDataQuality(unittest.TestCase):
    """P1-1: HTTP 成功但内容缺失时, 不标 ok / 不造假 0 / 图表断线。"""

    def _collector_with(self, responses):
        """responses: callable(n) -> metrics text for the n-th /metrics call."""
        calls = {"n": 0}
        real = sm.http_text

        def fake(path, base, timeout=5):
            calls["n"] += 1
            return responses(calls["n"])

        sm.http_text = fake
        self.addCleanup(setattr, sm, "http_text", real)
        return sm.Collector("http://x")

    def test_empty_metrics_not_ok_and_no_fake_zero(self):
        c = self._collector_with(lambda n: "")
        c._tick()
        s = c.snapshot()
        self.assertNotEqual(s["status"], "ok")
        self.assertIn("为空", s["status"])
        self.assertEqual(s["hist_decode"], [None])         # 断线, 不是 0
        self.assertEqual(s["hist_prefill"], [None])

    def test_partial_missing_only_affects_that_metric(self):
        seq = ["splash_decode_output_tokens_total 100\nsplash_decode_wall_milliseconds_total 200\n",
               "splash_decode_output_tokens_total 200\nsplash_decode_wall_milliseconds_total 400\n"]
        c = self._collector_with(lambda n: seq[min(n - 1, 1)])
        c._tick()
        c._tick()
        s = c.snapshot()
        self.assertEqual(s["status"], "ok")                # 部分缺失不整体判死
        self.assertAlmostEqual(s["hist_decode"][1], 500.0)  # 有效指标照常更新
        self.assertEqual(s["hist_prefill"], [None, None])  # 缺失指标 = None 断线
        self.assertTrue(s["decode_valid"])
        self.assertFalse(s["prefill_valid"])
        self.assertIsNone(s.get("mem_cur"))                # 内存不显示 0

    def test_idle_zero_is_real_and_kept(self):
        seq = ["splash_decode_output_tokens_total 10\nsplash_decode_wall_milliseconds_total 50\n"] * 2
        c = self._collector_with(lambda n: seq[min(n - 1, 1)])
        c._tick()
        c._tick()
        s = c.snapshot()
        self.assertEqual(s["hist_decode"][1], 0.0)         # 计数器在场且无活动 = 真 0
        self.assertTrue(s["decode_valid"])

    def test_drop_then_recovery(self):
        state = {"phase": 0}

        def resp(n):
            if state["phase"] == 0:
                return "splash_decode_output_tokens_total 100\nsplash_decode_wall_milliseconds_total 200\n"
            if state["phase"] == 1:
                return ""                                   # 引擎突然返回空 body
            return "splash_decode_output_tokens_total 300\nsplash_decode_wall_milliseconds_total 600\n"

        c = self._collector_with(resp)
        c._tick()
        state["phase"] = 1
        c._tick()
        state["phase"] = 2
        c._tick()
        s = c.snapshot()
        self.assertEqual(s["status"], "ok")                # 恢复后回到 ok
        self.assertEqual(s["hist_decode"][0], 0.0)
        self.assertIsNone(s["hist_decode"][1])             # 故障拍 = 断线
        self.assertAlmostEqual(s["hist_decode"][2], 500.0)  # 恢复: 200tok/0.4s
        self.assertAlmostEqual(s["dec_avg"], 500.0)

    def test_counters_present_but_empty_otherwise_memory_none(self):
        c = self._collector_with(lambda n: "splash_requests_completed_total 1\n")
        c._tick()
        s = c.snapshot()
        self.assertEqual(s["status"], "ok")
        self.assertIsNone(s.get("mem_cur"))
        self.assertFalse(s["decode_valid"])


class TestSnapshotIsolation(unittest.TestCase):
    """P2-1: snapshot 不得把活动历史列表引用交给 GUI。"""

    def test_history_copied_under_lock(self):
        c = sm.Collector("http://x")
        c._tick_i = 0
        c.state["hist_decode"] = [1.0, 2.0]
        s = c.snapshot()
        s["hist_decode"].append(99.0)                      # GUI 侧任意改动
        self.assertEqual(c.state["hist_decode"], [1.0, 2.0])  # 采集侧不受影响


class TestSystemMemMissingFields(unittest.TestCase):
    """P2-2: vm_stat 缺字段 => used None, 不拿 0 凑总和; 结构仍为三元组。"""

    def _patch(self, vmstat_text):
        import types

        real = sm.subprocess.run

        def fake(cmd, *a, **kw):
            if cmd[0].endswith("sysctl"):
                return types.SimpleNamespace(stdout="68719476736\n")   # 64 GB
            return types.SimpleNamespace(stdout=vmstat_text)

        sm.subprocess.run = fake
        self.addCleanup(setattr, sm, "subprocess_run_marker", None)     # no-op guard
        self.addCleanup(setattr, sm.subprocess, "run", real)

    VM_FULL = ("Mach Virtual Memory Statistics:\n"
               "Page size of bytes:     16384\n"
               "Pages free:                             100000\n"
               "Pages active:                           200000\n"
               "Pages inactive:                         150000\n"
               "Pages wired down:                       300000\n"
               "Pages occupied by compressor:           400000\n")

    def test_all_fields_present(self):
        self._patch(self.VM_FULL)
        used, total, ts = sm.system_mem()
        self.assertAlmostEqual(total, 64.0)
        self.assertAlmostEqual(used, (300000 + 200000 + 400000) * 16384 / 2**30)
        self.assertIsNotNone(ts)

    def test_missing_compressor_gives_none(self):
        self._patch(self.VM_FULL.replace("Pages occupied by compressor:           400000\n", ""))
        used, total, ts = sm.system_mem()
        self.assertIsNone(used)                            # 缺项 => None, 不是部分和
        self.assertAlmostEqual(total, 64.0)                # 结构稳定
        self.assertIsNotNone(ts)


class FakeResp:
    """Minimal stand-in for urlopen() response: context manager + iterable lines."""

    def __init__(self, body: bytes):
        self._b = body

    def read(self):
        return self._b

    def __iter__(self):
        return iter(self._b.splitlines())

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestBenchmarkOutcomes(unittest.TestCase):
    """P1-2: HTTP 200 != 成功; 成功/部分/失败分类 + 每轮重置 + 锁释放。"""

    SSTREAM_OK = (b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
                  b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
                  b'data: [DONE]\n\n')
    SSTREAM_EMPTY = (b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
                     b'data: [DONE]\n\n')
    M_IDLE = "splash_requests_completed_total 5\nsplash_requests_submitted_total 5\n"
    M_PRE = ["splash_prefill_input_tokens_total 0\nsplash_prefill_wall_milliseconds_total 0\n",
             "splash_prefill_input_tokens_total 2000\nsplash_prefill_wall_milliseconds_total 500\n"]
    M_DEC = ["splash_decode_output_tokens_total 10\nsplash_decode_wall_milliseconds_total 100\n",
             "splash_decode_output_tokens_total 310\nsplash_decode_wall_milliseconds_total 3100\n"]
    M_NONE = "splash_requests_completed_total 5\n"

    def _patch(self, seq, usage_fn, stream, raise_on=None):
        real = sm.urllib.request.urlopen
        calls = {"m": 0, "p": 0}

        def fake(req, timeout=None):
            url = req.full_url
            if url.endswith("/metrics"):
                if raise_on == "metrics":
                    raise ConnectionError("boom")
                i = min(calls["m"], len(seq) - 1)
                calls["m"] += 1
                return FakeResp(seq[i].encode())
            payload = json.loads(req.data.decode())
            if payload.get("stream"):
                return FakeResp(stream)
            calls["p"] += 1
            if raise_on == "post":
                raise ConnectionError("boom mid-run")
            usage = usage_fn(payload)
            body = {"choices": [{"message": {"content": "ok"}}]}
            if usage is not None:
                body["usage"] = usage
            return FakeResp(json.dumps(body).encode())

        sm.urllib.request.urlopen = fake
        self.addCleanup(setattr, sm.urllib.request, "urlopen", real)

    def _usage_full(self, payload):
        return {"prompt_tokens": 2000} if payload.get("max_tokens") == 2 else {"completion_tokens": 300}

    def _run(self, got):
        sm.run_benchmark("http://fake", lambda k, v: got.append((k, v)))
        return dict(got)

    def test_full_success(self):
        self._patch([self.M_IDLE] + self.M_PRE + self.M_DEC, self._usage_full, self.SSTREAM_OK)
        k = self._run([])
        self.assertTrue(str(k["bench_status"]).startswith("压测完成"), k["bench_status"])
        self.assertAlmostEqual(k["bench_prefill_tps"], 4000.0)
        self.assertEqual(k["bench_prefill_mode"], "官方阶段差分")
        self.assertAlmostEqual(k["bench_decode_tps"], 100.0)
        self.assertTrue(k["bench_ttft_ms"] > 0)
        self.assertIsNotNone(k["bench_ts"])
        self.assertEqual(k["bench_running"], False)

    def test_no_usage_no_counters_is_failure_not_complete(self):
        self._patch([self.M_NONE], lambda p: {} if p.get("max_tokens") else {}, self.SSTREAM_EMPTY)

        def no_usage(p):
            return {}
        self._patch([self.M_NONE], no_usage, self.SSTREAM_EMPTY)
        k = self._run([])
        self.assertTrue(str(k["bench_status"]).startswith("压测失败"), k["bench_status"])
        self.assertNotIn("压测完成", k["bench_status"])
        self.assertIsNone(k.get("bench_ts"))               # 失败不留"成功时间戳"
        self.assertIsNone(k.get("bench_prefill_tps"))
        self.assertIsNone(k.get("bench_decode_tps"))
        self.assertEqual(k["bench_running"], False)

    def test_partial_success_lists_missing(self):
        seq = [self.M_IDLE, self.M_NONE, self.M_NONE] + self.M_DEC   # prefill 计数器缺失
        self._patch(seq, lambda p: (None if p.get("max_tokens") == 2 else {"completion_tokens": 300}),
                    self.SSTREAM_OK)
        k = self._run([])
        self.assertTrue(str(k["bench_status"]).startswith("部分压测完成"), k["bench_status"])
        self.assertIn("PP 缺数据", k["bench_status"])
        self.assertAlmostEqual(k["bench_decode_tps"], 100.0)
        self.assertIsNone(k.get("bench_prefill_tps"))      # 缺数据不虚构吞吐

    def test_previous_results_reset_each_round(self):
        got = [("bench_decode_tps", 999.0), ("bench_prefill_tps", 888.0),
               ("bench_decode_mode", "官方阶段差分"), ("bench_ts", 12345.0)]
        self._patch([self.M_NONE], lambda p: {}, self.SSTREAM_EMPTY)
        k = self._run(got)
        self.assertIsNone(k["bench_decode_tps"])           # 旧结果已清
        self.assertIsNone(k["bench_prefill_tps"])
        self.assertIsNone(k.get("bench_ts"))
        self.assertTrue(str(k["bench_status"]).startswith("压测失败"))

    def test_failure_then_retry_success(self):
        self._patch([self.M_NONE], lambda p: {}, self.SSTREAM_EMPTY)
        k1 = self._run([])
        self.assertTrue(str(k1["bench_status"]).startswith("压测失败"))
        self._patch([self.M_IDLE] + self.M_PRE + self.M_DEC, self._usage_full, self.SSTREAM_OK)
        k2 = self._run([])
        self.assertTrue(str(k2["bench_status"]).startswith("压测完成"))
        self.assertAlmostEqual(k2["bench_decode_tps"], 100.0)

    def test_mid_run_exception_releases_lock(self):
        self._patch([self.M_IDLE], self._usage_full, self.SSTREAM_OK, raise_on="post")
        k = self._run([])
        self.assertEqual(k["bench_running"], False)        # finally 释放
        self.assertTrue(str(k["bench_status"]).startswith("压测失败"))

    def test_gui_shows_failure_without_timestamp(self):
        import inspect
        src = inspect.getsource(sm.gui_mode)
        # 状态栏渲染统一走 bench_display(纯函数), 不再内联在在线分支里
        self.assertIn("bench_display(s)", src)


class TestReleaseHygiene(unittest.TestCase):
    """P1-4 / P2-4: CI runner 与版本标识。"""

    def test_no_retired_runner(self):
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         ".github", "workflows", "ci.yml")
        yml = open(p).read()
        self.assertNotIn("macos-14", yml)
        self.assertIn("macos-15", yml)
        self.assertIn("arm64", yml)                        # 架构显式断言, 不信标签默认

    def test_title_version_matches_release(self):
        import inspect
        src = inspect.getsource(sm.gui_mode)
        self.assertIn('title("Splash Monitor v1.0.0")', src)
        self.assertNotIn("v2", src.split("mainloop")[0].split("title")[1][:80])


class TestBenchDisplay(unittest.TestCase):
    """P2: 状态栏渲染逻辑(bench_display)不依赖引擎在线状态, 直接验证真实渲染输出。"""

    FAIL_TXT = "压测失败: URLError: 模拟断线"
    DONE_TXT = "压测完成 12:00:00"

    def test_online_benchmark_success(self):
        btn, lbl = sm.bench_display({
            "status": "ok", "bench_ts": 1.0, "bench_status": self.DONE_TXT,
            "bench_decode_tps": 100.0, "bench_prefill_tps": 4000.0, "bench_ttft_ms": 250.0})
        self.assertEqual(btn, "▶ Benchmark")
        self.assertIn("压测完成", lbl)
        self.assertIn("TG 100", lbl)

    def test_offline_failure_is_rendered(self):
        # 引擎离线 + 压测失败: 失败原因必须渲染(原 bug: 在线分支内, 断线不显示)
        btn, lbl = sm.bench_display({"status": "连不上 http://x (URLError)",
                                     "bench_status": self.FAIL_TXT, "bench_ts": None})
        self.assertEqual(lbl, self.FAIL_TXT)

    def test_offline_running_is_rendered(self):
        btn, lbl = sm.bench_display({"status": "连不上 http://x (URLError)",
                                     "bench_running": True})
        self.assertEqual(btn, "⏳ 压测中…")
        self.assertIn("压测进行中", lbl)

    def test_first_round_failure(self):
        btn, lbl = sm.bench_display({"status": "ok", "bench_status": self.FAIL_TXT})
        self.assertEqual(lbl, self.FAIL_TXT)

    def test_recovery_after_outage(self):
        state = {"status": "连不上", "bench_status": self.FAIL_TXT, "bench_ts": None}
        self.assertEqual(sm.bench_display(state)[1], self.FAIL_TXT)   # 断线: 显示失败
        state.update({"status": "ok", "bench_status": self.DONE_TXT, "bench_ts": 9.9,
                      "bench_decode_tps": 88.0, "bench_prefill_tps": 3000.0,
                      "bench_ttft_ms": 300.0})
        self.assertIn("压测完成", sm.bench_display(state)[1])          # 恢复: 回到结果行

    def test_running_wins_over_old_results(self):
        btn, lbl = sm.bench_display({"bench_running": True, "bench_ts": 1.0,
                                     "bench_status": self.DONE_TXT,
                                     "bench_decode_tps": 100.0})
        self.assertEqual(btn, "⏳ 压测中…")
        self.assertIn("压测进行中", lbl)
        self.assertNotIn("TG 100", lbl)                                # 旧数字不盖住进行中

    def test_never_benchmarked_is_empty(self):
        btn, lbl = sm.bench_display({"status": "ok"})
        self.assertEqual(btn, "▶ Benchmark")
        self.assertEqual(lbl, "")                                      # 保持原有初始空白行为

    def test_render_hook_is_outside_online_branch(self):
        import inspect
        src = inspect.getsource(sm.gui_mode)
        # 结构守卫: 渲染调用在 refresh 函数体层级(8 空格), 不在 if/else 分支内
        self.assertIn("\n        btn, lbl = bench_display(s)", src)
        self.assertNotIn("⏳ 压测中…", src)                             # 旧内联分支已移除
