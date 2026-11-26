"""Unit tests for splash_monitor — no GUI, no running engine required.

Run:  python -m unittest discover -s tests -v
"""
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
