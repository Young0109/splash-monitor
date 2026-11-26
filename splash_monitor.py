"""
Splash Monitor — lightweight real-time dashboard for the Splash local LLM
inference engine on Apple Silicon (independent, unofficial tool).

Shows: Decode / Prefill / TTFT / Memory (engine + system + swap) / GPU.

Data sources (official engine counters first; no guessing):
  - /metrics  official counters: ttft_p50/p95, itl_p50, decode/prefill
              tokens_total with wall_milliseconds_total,
              memory_current/peak/limit_bytes, etc.
              Live rate = dTokens / d(engine-reported wall clock), so it is
              not distorted by the sampling window or batched commits.
  - /status   model context limit and engine state (low frequency).
  - ioreg     GPU utilization — note: on Apple Silicon this IORegistry
              counter is the sum of ALL processes (system-wide), labeled
              as such in the UI.
  - memory_pressure -Q / sysctl vm.swapusage: macOS system memory free
              percentage and swap. No public API exposes a system-wide
              pressure "level" (kern.memorystatus_vm_pressure_level is
              per-process and always "normal" for us), so no level is
              inferred.

Usage: python3 splash_monitor.py [--url URL] [--port N] [--cli]
Endpoint defaults to http://127.0.0.1:8000.
Standard library only; requires macOS.
"""
import argparse
import json
import random
import re
import string
import subprocess
import sys
import threading
import time
import urllib.request

ENDPOINT_DEFAULT = "http://127.0.0.1:8000"


def make_base(port=None, url=None):
    """归一化引擎端点: --url 优先; 默认本机 8000。"""
    if url:
        return url.rstrip("/")
    if port:
        return "http://127.0.0.1:%d" % port
    return ENDPOINT_DEFAULT

# /metrics 里直接取用的官方字段
KEY_TTFT_P50 = "ttft_p50_milliseconds"
KEY_TTFT_P95 = "ttft_p95_milliseconds"
KEY_ITL_P50 = "itl_p50_milliseconds"
KEY_DEC_TOKENS = "decode_output_tokens_total"
KEY_DEC_WALL = "decode_wall_milliseconds_total"
KEY_PRE_TOKENS = "prefill_input_tokens_total"
KEY_PRE_WALL = "prefill_wall_milliseconds_total"
KEY_MEM_CUR = "memory_current_bytes"
KEY_MEM_PEAK = "memory_peak_bytes"
KEY_MEM_LIM = "memory_limit_bytes"
KEY_REQ_DONE = "requests_completed_total"
KEY_ACCEPT = "draft_acceptance_ratio"


# ---------------- 数据采集 ----------------

def http_text(path, base, timeout=5):
    req = urllib.request.Request(f"{base}{path}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()


def parse_metrics(text):
    """解析 Prometheus 文本: 无 label 的标量 + 少数带 label 的目标行。"""
    out = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        pm = re.match(r'splash_memory_pressure\{state="(\w+)"\} ([\d.]+)$', s)
        if pm and float(pm.group(2)) > 0:
            out["pressure_engine"] = pm.group(1)
            continue
        th = re.match(r'splash_thermal_state\{state="(\w+)"\} ([\d.]+)$', s)
        if th and float(th.group(2)) > 0:
            out["thermal"] = th.group(1)
            continue
        m = re.match(r"splash_(\w+)\s+([\d.eE+-]+)$", s)
        if m:
            out[m.group(1)] = float(m.group(2))
    return out


def rate_from_delta(prev_m, met, tok_key, wall_key):
    """实时速率 = Δtokens / Δ引擎墙钟。
    语义是"引擎实际干活时间的吞吐": 分母只用 Δwall 本身, 不用采样窗口
    去截断——计数器按批提交, 晚提交时窗口比引擎时间短, 截断会虚高速率;
    Δwall 作为引擎自报的工作时长, 本身就是正确分母。
    返回 (速率, 本窗口是否有活动); 计数器倒退(引擎重启)返回 (None, False)。"""
    if not prev_m:
        return None, False
    t0, t1 = prev_m.get(tok_key), met.get(tok_key)
    w0, w1 = prev_m.get(wall_key), met.get(wall_key)
    if None in (t0, t1, w0, w1):
        return None, False
    d_tok, d_wall = t1 - t0, (w1 - w0) / 1000.0
    if d_tok < 0 or d_wall < 0:
        return None, False          # 计数器被重置(引擎重启), 交给上层处理
    if d_tok == 0 or d_wall <= 0.01:
        return None, False
    return d_tok / max(d_wall, 0.05), True


def avg_rate(tok_total, wall_ms_total):
    """官方 lifetime 平均 = 累计 tokens / 累计引擎墙钟 (tokens/s)。
    任一输入缺失或墙钟 <= 0 返回 None —— 不把缺失当 0。
    注意与 ITL p50 换算率 (1000/itl_p50) 的区别: 后者是单 token 间隔
    中位数的倒数, 只是参考换算值, 不是全体请求的平均吞吐。"""
    if tok_total is None or wall_ms_total is None:
        return None
    w = wall_ms_total / 1000.0
    if w <= 0:
        return None
    return tok_total / w


def gpu_stats():
    """系统级 GPU 计数器(所有进程合计), 来自 IORegistry, 免 root。"""
    try:
        raw = subprocess.run(["/usr/sbin/ioreg", "-r", "-c", "IOAccelerator"],
                             capture_output=True, text=True, timeout=5).stdout
        m = re.search(r'"Device Utilization %"=(\d+)', raw)
        r = re.search(r'"Renderer Utilization %"=(\d+)', raw)
        u = re.search(r'"In use system memory"=(\d+)', raw)
        return {"device": int(m.group(1)) if m else None,
                "renderer": int(r.group(1)) if r else None,
                "vram_bytes": int(u.group(1)) if u else None,
                "ts": time.time()}
    except Exception:
        return {"device": None, "renderer": None, "vram_bytes": None, "ts": None}


def system_mem():
    """(used_gb, total_gb) 来自 vm_stat; wired+active+compressor。"""
    try:
        total = float(subprocess.run(["/usr/sbin/sysctl", "-n", "hw.memsize"],
                                     capture_output=True, text=True, timeout=3).stdout) / (1 << 30)
        raw = subprocess.run(["/usr/bin/vm_stat"], capture_output=True, text=True, timeout=3).stdout
        pm = re.search(r"page size of (\d+)", raw)
        page = int(pm.group(1)) if pm else 16384

        def f(name):
            m = re.search(rf"{name}\s*:\s*(\d+)", raw)
            return int(m.group(1)) * page / (1 << 30) if m else None
        parts = [f("Pages wired down"), f("Pages active"),
                 f("Pages occupied by compressor")]
        # 任一字段缺失 => used 为 None (不拿假 0 凑总和); 返回三元组结构保持稳定
        used = sum(parts) if all(p is not None for p in parts) else None
        return used, total, time.time()
    except Exception:
        return None, None, None


def pressure_and_swap():
    """macOS 官方口径的 free percentage 与 Swap 用量。
    注意: macOS 不暴露系统级内存压力"等级"(normal/warning/critical) 的
    公开接口(kern.memorystatus_vm_pressure_level 是按调用进程视角返回的,
    对本进程恒为 normal, 不代表整机), 因此只保留原始百分比, 等级标注未知,
    不做任何颜色/等级推断。"""
    free_pct = None
    try:
        raw = subprocess.run(["/usr/bin/memory_pressure", "-Q"], capture_output=True, text=True, timeout=4).stdout
        m = re.search(r"free percentage:\s*(\d+)%", raw)
        if m:
            free_pct = int(m.group(1))
    except Exception:
        pass
    swap_used, swap_total = None, None
    try:
        raw = subprocess.run(["/usr/sbin/sysctl", "-n", "vm.swapusage"],
                             capture_output=True, text=True, timeout=3).stdout
        mu = re.search(r"used\s*=\s*([\d.]+)M", raw)
        mt = re.search(r"total\s*=\s*([\d.]+)M", raw)
        if mu:
            swap_used = float(mu.group(1)) / 1024
        if mt:
            swap_total = float(mt.group(1)) / 1024
    except Exception:
        pass
    return {"free_pct": free_pct, "level": None,
            "swap_used_gb": swap_used, "swap_total_gb": swap_total, "ts": time.time()}


def engine_status(base):
    try:
        d = json.loads(http_text("/status", base, timeout=4))
        return {"ready": d.get("ready"), "max_ctx": d.get("maximum_context_tokens"),
                "thermal": d.get("thermal_state"), "pressure_engine": d.get("memory_pressure"),
                "ts": time.time()}
    except Exception:
        return None


# ---------------- Benchmark(实测值, 与实时估算区分) ----------------

def _snap_stage(base, keys):
    m = parse_metrics(http_text("/metrics", base, timeout=4))
    return {k: m.get(k) for k in keys}


def _stage_rate(a, b, tok_key, wall_key):
    """两次 /metrics 快照间的官方阶段吞吐 = Δtokens / Δ引擎墙钟。"""
    try:
        dt = b[tok_key] - a[tok_key]
        dw = (b[wall_key] - a[wall_key]) / 1000.0
        if dt > 0 and dw > 0.01:
            return dt / dw
    except (TypeError, KeyError):
        pass
    return None


def run_benchmark(base, on_update):
    """顺序: 等空闲 -> TTFT -> Prefill(阶段差分+端到端) -> Decode(同)。
    阶段差分基于官方计数器, 若期间混入其他请求(Δtokens 明显超出本次请求
    usage), 结果标注污染并回退到端到端估算。"""
    def post(payload, timeout=600):
        req = urllib.request.Request(
            f"{base}/v1/chat/completions",
            data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read())
        return body, time.time() - t0

    notes = []
    # 每轮开始先重置上一轮结果: 失败/首轮时 GUI 不会残留旧数字
    for k in ("bench_ttft_ms", "bench_prefill_tps", "bench_decode_tps",
              "bench_prefill_mode", "bench_decode_mode", "bench_ts"):
        on_update(k, None)
    pre_done = dec_done = False
    try:
        # 0) 等引擎空闲, 避免把别人的请求混进阶段差分 (最多等 10s)
        t_end = time.time() + 10
        while time.time() < t_end:
            m = parse_metrics(http_text("/metrics", base, timeout=4))
            busy = (m.get("scheduler_queued", 0) > 0
                    or m.get("scheduler_prefilling", 0) > 0
                    or m.get("scheduler_decoding", 0) > 0
                    or m.get("requests_submitted_total", 0) - m.get("requests_completed_total", 0) > 0)
            if not busy:
                break
            time.sleep(0.5)
        else:
            notes.append("未等到空闲")

        # 1) TTFT: 首个"非空" content/reasoning_content 的到达时间。
        #    只有 role 的空 delta 是建流事件, 不算首 token。
        req = urllib.request.Request(
            f"{base}/v1/chat/completions",
            data=json.dumps({"messages": [{"role": "user", "content": "Say hello in one word."}],
                             "max_tokens": 4, "temperature": 0, "stream": True}).encode(),
            headers={"Content-Type": "application/json"})
        t0 = time.time()
        ttft = None
        with urllib.request.urlopen(req, timeout=60) as r:
            for line in r:
                s = line.decode(errors="ignore").strip()
                if s.startswith("data:") and "[DONE]" not in s:
                    try:
                        d = json.loads(s[5:].strip())
                        delta = d.get("choices", [{}])[0].get("delta", {})
                        if delta.get("content") or delta.get("reasoning_content"):
                            ttft = time.time() - t0
                            break
                    except Exception:
                        pass
        if ttft:
            on_update("bench_ttft_ms", ttft * 1000)

        # 2) Prefill: 随机词绕开前缀缓存。主口径=官方阶段计数器差分。
        rnd = random.Random()
        alpha = string.ascii_lowercase
        words = " ".join("".join(rnd.choice(alpha) for _ in range(rnd.randint(3, 9)))
                         for _ in range(4000))
        prompt = ("Repeat these nonsense words exactly (output only 'ok'):\n"
                  + words + "\nNow say ok.")
        a = _snap_stage(base, (KEY_PRE_TOKENS, KEY_PRE_WALL))
        body, dt = post({"messages": [{"role": "user", "content": prompt}],
                         "max_tokens": 2, "temperature": 0})
        b = _snap_stage(base, (KEY_PRE_TOKENS, KEY_PRE_WALL))
        pt = body.get("usage", {}).get("prompt_tokens", 0)
        e2e = pt / dt if pt and dt > 0 else None
        stage = _stage_rate(a, b, KEY_PRE_TOKENS, KEY_PRE_WALL)
        d_tok = (b[KEY_PRE_TOKENS] - a[KEY_PRE_TOKENS]) if None not in (a.get(KEY_PRE_TOKENS), b.get(KEY_PRE_TOKENS)) else None
        if stage and d_tok is not None and pt and d_tok > pt * 1.3:
            stage = None
            notes.append("PP受并发污染")
        if stage:
            on_update("bench_prefill_tps", stage)
            on_update("bench_prefill_mode", "官方阶段差分")
            pre_done = True
        elif e2e:
            on_update("bench_prefill_tps", e2e)
            on_update("bench_prefill_mode", "端到端估算")
            pre_done = True

        # 3) Decode: 同样阶段差分为先。
        a = _snap_stage(base, (KEY_DEC_TOKENS, KEY_DEC_WALL))
        body, dt = post({"messages": [{"role": "user",
                                       "content": "List 300 unusual compound words, one per line, nothing else."}],
                         "max_tokens": 300, "temperature": 0.7})
        b = _snap_stage(base, (KEY_DEC_TOKENS, KEY_DEC_WALL))
        ct = body.get("usage", {}).get("completion_tokens", 0)
        e2e = ct / dt if ct and dt > 0 else None
        stage = _stage_rate(a, b, KEY_DEC_TOKENS, KEY_DEC_WALL)
        d_tok = (b[KEY_DEC_TOKENS] - a[KEY_DEC_TOKENS]) if None not in (a.get(KEY_DEC_TOKENS), b.get(KEY_DEC_TOKENS)) else None
        if stage and d_tok is not None and ct and d_tok > ct * 1.3:
            stage = None
            notes.append("TG受并发污染")
        if stage:
            on_update("bench_decode_tps", stage)
            on_update("bench_decode_mode", "官方阶段差分")
            dec_done = True
        elif e2e:
            on_update("bench_decode_tps", e2e)
            on_update("bench_decode_mode", "端到端估算")
            dec_done = True

        # 成功判定以"拿到可计算的数据"为准, HTTP 200 本身不算成功:
        # 三项全无 => 失败; 部分缺失 => 部分完成并列出缺项; 三项齐全 => 完成。
        missing = [n for n, ok in (("TTFT", bool(ttft)), ("PP", pre_done), ("TG", dec_done))
                   if not ok]
        if not missing:
            on_update("bench_ts", time.time())
            on_update("bench_status", "压测完成 " + time.strftime("%H:%M:%S")
                      + ((" (" + "; ".join(notes) + ")") if notes else ""))
        elif len(missing) < 3:
            on_update("bench_ts", time.time())
            on_update("bench_status", "部分压测完成 " + time.strftime("%H:%M:%S")
                      + " (" + ",".join(missing) + " 缺数据"
                      + ("; " + "; ".join(notes) if notes else "") + ")")
        else:
            on_update("bench_status",
                      "压测失败: 未获得有效结果 (引擎未返回 usage 且无阶段计数器"
                      + ("; " + "; ".join(notes) if notes else "") + ")")
    except Exception as e:
        on_update("bench_status", f"压测失败: {type(e).__name__}: {e}")
    finally:
        on_update("bench_running", False)


# ---------------- 采集循环 ----------------

INTERVAL = 1.5          # /metrics 采样
GPU_EVERY = 2           # 每 N 拍刷一次 ioreg (较重)
SYS_EVERY = 3           # 每 N 拍刷一次 memory_pressure + swap
STATUS_EVERY = 6        # 每 N 拍刷一次 /status
STALE = 8.0             # 数据超过 N 秒视为过期


class Collector:
    def __init__(self, base):
        self.base = base
        self.state = {"status": "connecting", "hist_decode": [], "hist_prefill": [], "hist_gpu": [],
                      "hist_sysmem": [],
                      "bench_running": False, "bench_status": "", "bench_ts": None,
                      "decode_tps": None, "prefill_tps": None, "prefill_prog": False,
                      "decode_bench": None, "prefill_bench": None}
        self.lock = threading.Lock()
        self._prev_met = None
        self._prev_t = None
        self._last_req_done = None
        self._restarts = 0
        self._tick_i = 0
        self._stop = threading.Event()

    def snapshot(self):
        """给 GUI 的一致性快照: 锁内浅拷贝 + 历史列表副本,
        避免采样线程 append 与 Tk 主线程绘图共用同一 list。"""
        with self.lock:
            s = dict(self.state)
            for h in ("hist_decode", "hist_prefill", "hist_gpu", "hist_sysmem"):
                s[h] = list(s.get(h) or [])
            return s

    def update(self, key, val):
        with self.lock:
            self.state[key] = val

    def try_start_bench(self):
        """原子地 check-and-set bench_running, 杜绝快速双击时两个线程
        同时通过检查的竞态。成功启动返回 True。"""
        with self.lock:
            if self.state.get("bench_running"):
                return False
            self.state["bench_running"] = True
            self.state["bench_status"] = ""
        threading.Thread(target=run_benchmark, args=(self.base, self.update),
                         daemon=True).start()
        return True

    def _tick(self):
        self._tick_i += 1
        try:
            met = parse_metrics(http_text("/metrics", self.base, timeout=4))
            now = time.time()
            if not met:
                # HTTP 成功但 body 为空/不可解析: 不标 ok、不造数据;
                # 引擎侧历史记 None(图上断线), 系统级历史沿用最近一次采集。
                with self.lock:
                    s = self.state
                    s["status"] = "metrics 为空或不可解析(引擎版本?)"
                    g = s.get("gpu") or {}
                    smem = s.get("sysmem")
                    s["hist_decode"].append(None)
                    s["hist_prefill"].append(None)
                    s["hist_gpu"].append(g.get("device"))
                    s["hist_sysmem"].append(
                        (smem[0] / smem[1]) if (smem and smem[0] is not None and smem[1]) else None)
                    for h in ("hist_decode", "hist_prefill", "hist_gpu", "hist_sysmem"):
                        del s[h][:-90]
                return

            # 引擎重启检测: 单调计数器倒退
            req_done = met.get(KEY_REQ_DONE)
            if req_done is not None and self._last_req_done is not None and req_done < self._last_req_done:
                self._restarts += 1
                with self.lock:
                    self.state["restarted_at"] = now
                self._prev_met, self._prev_t = None, None
            if req_done is not None:
                self._last_req_done = req_done

            dec_tps, dec_act = rate_from_delta(self._prev_met, met, KEY_DEC_TOKENS, KEY_DEC_WALL)
            pre_tps, pre_act = rate_from_delta(self._prev_met, met, KEY_PRE_TOKENS, KEY_PRE_WALL)
            # 计数器在场与否决定"0"是否可信: 在场=真实空闲(0), 缺失=None(断线)
            dec_ok = KEY_DEC_TOKENS in met and KEY_DEC_WALL in met
            pre_ok = KEY_PRE_TOKENS in met and KEY_PRE_WALL in met
            self._prev_met, self._prev_t = met, now

            if self._tick_i % GPU_EVERY == 1 or self.state.get("gpu") is None:
                self.update("gpu", gpu_stats())
            if self._tick_i % SYS_EVERY == 1 or self.state.get("sysps") is None:
                self.update("sysps", pressure_and_swap())
                self.update("sysmem", system_mem())
            if self._tick_i % STATUS_EVERY == 1 or self.state.get("engine_status") is None:
                self.update("engine_status", engine_status(self.base))

            with self.lock:
                s = self.state
                s["status"] = "ok"
                s["met_ts"] = now
                s["decode_tps"] = dec_tps
                s["decode_active"] = bool(met.get("scheduler_decoding")) or dec_act
                s["prefill_tps"] = pre_tps
                s["prefill_prog"] = bool(met.get("scheduler_prefilling")) or pre_act
                if pre_tps:
                    s["prefill_last"] = pre_tps
                    s["prefill_last_ts"] = now
                s["hist_decode"].append(dec_tps if dec_tps is not None else (0.0 if dec_ok else None))
                s["hist_prefill"].append(pre_tps if pre_tps is not None else (0.0 if pre_ok else None))
                s["decode_valid"] = dec_ok
                s["prefill_valid"] = pre_ok
                g = s.get("gpu") or {}
                s["hist_gpu"].append(g.get("device"))
                # 系统内存占用比例历史 (0..1); 采集失败记 None → 图上断线, 不画假数据
                sm = s.get("sysmem")
                s["hist_sysmem"].append(
                    (sm[0] / sm[1]) if (sm and sm[0] is not None and sm[1]) else None)
                for h in ("hist_decode", "hist_prefill", "hist_gpu", "hist_sysmem"):
                    del s[h][:-90]
                s["ttft_p50"] = met.get(KEY_TTFT_P50)
                s["ttft_p95"] = met.get(KEY_TTFT_P95)
                s["itl_p50"] = met.get(KEY_ITL_P50)
                s["dec_avg"] = avg_rate(met.get(KEY_DEC_TOKENS), met.get(KEY_DEC_WALL))
                s["pre_avg"] = avg_rate(met.get(KEY_PRE_TOKENS), met.get(KEY_PRE_WALL))
                s["accept_ratio"] = met.get(KEY_ACCEPT)
                s["mem_cur"] = met.get(KEY_MEM_CUR)
                s["mem_peak"] = met.get(KEY_MEM_PEAK)
                s["mem_lim"] = met.get(KEY_MEM_LIM)
                s["pressure_engine"] = met.get("pressure_engine")
                s["thermal"] = met.get("thermal")
                s["queued"] = met.get("scheduler_queued")
                s["running"] = (met.get("requests_submitted_total", 0)
                                - met.get("requests_completed_total", 0)) > 0
                s["ts"] = time.strftime("%H:%M:%S")
        except Exception as e:
            with self.lock:
                self.state["status"] = f"连不上 {self.base} ({type(e).__name__})"

    def loop(self):
        while not self._stop.is_set():
            t0 = time.time()
            self._tick()
            self._stop.wait(max(0.2, INTERVAL - (time.time() - t0)))

    def start(self):
        threading.Thread(target=self.loop, daemon=True).start()


def fresh(ts, limit=STALE):
    return ts is not None and (time.time() - ts) <= limit


def fmt(v, pat="{:.1f}"):
    return pat.format(v) if v is not None else "--"


# ---------------- CLI ----------------

def cli_mode(base):
    c = Collector(base)
    c.start()
    time.sleep(4)
    s = c.snapshot()
    if s["status"] != "ok":
        print(f"状态: {s['status']}")
        return 1
    g = s.get("gpu") or {}
    sysps = s.get("sysps") or {}
    used, total, _ = s.get("sysmem") or (None, None, None)
    print(f"状态     : ok  引擎时间 {s['ts']}  排队 {fmt(s.get('queued'), '{:.0f}')}")
    # 官方均值 = 累计 tokens / 累计引擎墙钟 (dec_avg)。不可得时回退
    # ITL p50 换算, 并明确标注为换算参考, 不冒称"平均"。
    avg = s.get("dec_avg")
    if avg is None and s.get("itl_p50"):
        avg_txt = f"ITL p50换算参考 {fmt(1000/s['itl_p50'])} tok/s(非平均值)"
    else:
        avg_txt = f"官方均值 {fmt(avg)} tok/s"
    print(f"Decode   : 实时 {fmt(s.get('decode_tps'))} tok/s | {avg_txt} "
          f"| Benchmark {fmt(s.get('bench_decode_tps'))} [{s.get('bench_decode_mode') or '未测'}]")
    print(f"Prefill  : {'进行中' if s.get('prefill_prog') else '空闲'}  实时 {fmt(s.get('prefill_tps'), '{:.0f}')} "
          f"| Benchmark {fmt(s.get('bench_prefill_tps'), '{:.0f}')} [{s.get('bench_prefill_mode') or '未测'}]")
    print(f"TTFT     : 官方 p50 {fmt(s.get('ttft_p50'), '{:.0f}')} ms / p95 {fmt(s.get('ttft_p95'), '{:.0f}')} ms "
          f"| Benchmark {fmt(s.get('bench_ttft_ms'), '{:.0f}')} ms (首个非空token)")
    cur, lim, peak = s.get("mem_cur"), s.get("mem_lim"), s.get("mem_peak")
    print(f"内存     : 引擎 {fmt(cur/2**30 if cur is not None else None)} / "
          f"{fmt(lim/2**30 if lim is not None else None, '{:.0f}')} GB "
          f"(峰值 {fmt(peak/2**30 if peak is not None else None)})  引擎压力 {s.get('pressure_engine','?')} (官方)")
    print(f"系统内存 : {fmt(used)} / {fmt(total, '{:.0f}')} GB  "
          f"系统free {sysps.get('free_pct','?')}% 压力等级未知(无公开API)  "
          f"Swap {fmt(sysps.get('swap_used_gb'))}/{fmt(sysps.get('swap_total_gb'), '{:.0f}')} GB")
    print(f"GPU(系统级): {fmt(g.get('device'), '{:.0f}')}% (renderer {fmt(g.get('renderer'), '{:.0f}')}%) "
          f"显存 {fmt((g.get('vram_bytes') or 0)/2**30 if g.get('vram_bytes') else None)} GB")
    return 0


# ---- 活动监视器风格图表: 横向铺满, 零值贴底基线, None 断线, 不平滑 ----
def _chart_geom(cv, hist, vmax, n_keep=90):
    pts = hist[-n_keep:]
    if not pts:
        return None
    top = vmax or max((v for v in pts if v), default=1) or 1
    n = len(pts)
    w, h = cv.winfo_width(), cv.winfo_height()
    if w < 20 or h < 10:
        return None
    x0, x1 = 1.0, float(w) - 1.0
    step = (x1 - x0) / (n - 1) if n > 1 else 0.0
    base, span = float(h) - 1.0, float(h) - 4.0
    return pts, top, x0, step, base, span

def _flush_seg(cv, seg, color, base=None, fill=None):
    if len(seg) == 1:
        x, y = seg[0]
        cv.create_line(x, y, x + 0.01, y, fill=color, width=1.2, capstyle="round")
    elif len(seg) >= 2:
        flat = [c for xy in seg for c in xy]
        if fill is not None:
            closed = flat + [seg[-1][0], base, seg[0][0], base]
            cv.create_polygon(closed, smooth=False, fill=fill, outline="")
        cv.create_line(*flat, fill=color, width=1.2)

def draw_line(cv, hist, color, vmax=None):
    cv.delete("all")
    geom = _chart_geom(cv, hist, vmax)
    if not geom:
        return
    pts, top, x0, step, base, span = geom
    seg = []
    for i, v in enumerate(pts):
        if v is None:
            _flush_seg(cv, seg, color)
            seg.clear()
        else:
            seg.append((x0 + i * step, base - min(max(v, 0.0) / top, 1.0) * span))
    _flush_seg(cv, seg, color)

def draw_area(cv, hist, edge, fill, vmax=None):
    cv.delete("all")
    geom = _chart_geom(cv, hist, vmax)
    if not geom:
        return
    pts, top, x0, step, base, span = geom
    seg = []
    for i, v in enumerate(pts):
        if v is None:
            _flush_seg(cv, seg, edge, base, fill)
            seg.clear()
        else:
            seg.append((x0 + i * step, base - min(max(v, 0.0) / top, 1.0) * span))
    _flush_seg(cv, seg, edge, base, fill)


# ---------------- GUI ----------------

def bench_display(s):
    """Benchmark 状态栏渲染逻辑(纯函数): 返回 (按钮文本, 状态文本)。
    优先级: 进行中 > 有结果(完成/部分, 带时间戳) > 失败原因 > 尚未压测。
    与引擎在线/离线状态无关 —— 断线导致的压测失败同样要显示出来。"""
    if s.get("bench_running"):
        extra = f"  {s['bench_status']}" if s.get("bench_status") else ""
        return "⏳ 压测中…", "压测进行中, 其他请求会排队" + extra
    if s.get("bench_ts"):
        return "▶ Benchmark", (f"{s.get('bench_status', '')}  "
                               f"TG {fmt(s.get('bench_decode_tps'))}  "
                               f"PP {fmt(s.get('bench_prefill_tps'), '{:.0f}')}  "
                               f"TTFT {fmt(s.get('bench_ttft_ms'), '{:.0f}')}ms")
    if s.get("bench_status"):
        # 首轮即失败/断线导致失败(无成功时间戳): 原因必须显示, 不能沉默
        return "▶ Benchmark", str(s["bench_status"])
    return "▶ Benchmark", ""


def gui_mode(base):
    import tkinter as tk

    # 浅色主题
    DARK, CARD, TXT, DIM = "#f5f5f7", "#ffffff", "#1d1d1f", "#6e6e73"
    OK, WARN, BAD = "#248a3d", "#b25000", "#d70015"
    # 卡片强调色: 白底上的深色系, 保证可读
    ACCENT = ("#007aff", "#a5730a", "#c93400", "#248a3d")
    # 内存图: 活动监视器式柔和绿; 配额条: 柔和青蓝 (配额高≠系统压力, 不用红色)
    G_EDGE, G_FILL, TEAL = "#5cb26e", "#c4ecca", "#33a8c4"

    c = Collector(base)
    c.start()

    root = tk.Tk()
    root.title("Splash Monitor v1.0.0")
    root.configure(bg=DARK)
    root.geometry("720x420")
    root.minsize(640, 380)

    def round_rect(cv, x0, y0, x1, y1, r, fill, outline):
        pts = [x0+r, y0, x1-r, y0, x1, y0, x1, y0+r, x1, y1-r, x1, y1,
               x1-r, y1, x0+r, y1, x0, y1, x0, y1-r, x0, y0+r, x0, y0]
        cv.create_polygon(pts, smooth=True, fill=fill, outline=outline,
                          width=1, tags="card")

    INSET = 3
    CH = 46   # 趋势区高度: 四卡一致, 全部 side=bottom 贴底 → 区域完全对齐

    def make_card(parent, title, grid_xy, color, with_bar=False):
        """圆角卡片: Canvas 画白色圆角底, 内容 Frame 内嵌其上。
        布局: 标题/大字/小字 从上往下; 趋势图 side=bottom 贴底固定;
        with_bar 时引擎配额条夹在两者之间。"""
        cv = tk.Canvas(parent, bg=DARK, highlightthickness=0)
        cv.grid(row=grid_xy[0], column=grid_xy[1], sticky="nsew", padx=5, pady=5)
        f = tk.Frame(cv, bg=CARD, bd=0)
        win = cv.create_window(0, 0, anchor="nw", window=f)

        def draw(evt=None):
            w, h = cv.winfo_width(), cv.winfo_height()
            if w < 10 or h < 10:
                return
            cv.delete("card")
            round_rect(cv, 1, 1, w - 2, h - 2, 16, CARD, "#e6e6ea")
            cv.coords(win, INSET, INSET)
            cv.itemconfig(win, width=w - 2 * INSET, height=h - 2 * INSET)
            cv.tag_raise(win)

        cv.bind("<Configure>", draw)
        cv.after(50, draw)
        # 趋势区先 pack(side="bottom") → 永远贴底且四卡同高同位
        chart = tk.Canvas(f, height=CH, bg=CARD, highlightthickness=0)
        chart.pack(side="bottom", fill="x", padx=12, pady=(0, 9))
        qcv = qpc = None
        if with_bar:
            brow = tk.Frame(f, bg=CARD)
            brow.pack(side="bottom", fill="x", padx=14, pady=(2, 1))
            tk.Label(brow, text="引擎配额", bg=CARD, fg=DIM, font=("Menlo", 9)).pack(side="left")
            qpc = tk.Label(brow, text="--", bg=CARD, fg=DIM, font=("Menlo", 9))
            qpc.pack(side="right")
            qcv = tk.Canvas(brow, height=10, bg=CARD, highlightthickness=0)
            qcv.pack(side="left", fill="x", expand=True, padx=8)
        tk.Label(f, text=title, bg=CARD, fg=color, font=("Menlo", 11), anchor="w").pack(fill="x", padx=14, pady=(9, 0))
        big = tk.Label(f, text="--", bg=CARD, fg=TXT, font=("Menlo Bold", 26))
        big.pack(anchor="w", padx=14)
        sub = tk.Label(f, text="", bg=CARD, fg=DIM, font=("Menlo", 10), anchor="w",
                       justify="left", wraplength=300)
        sub.pack(anchor="w", padx=14, pady=(0, 1))
        return f, big, sub, chart, qcv, qpc

    grid = tk.Frame(root, bg=DARK)
    grid.pack(fill="both", expand=True)
    # uniform 分组: 强制两列/两行等宽等高, 长文本不会再撑歪布局
    for i in (0, 1):
        grid.columnconfigure(i, weight=1, uniform="col")
        grid.rowconfigure(i, weight=1, uniform="row")

    _, d_big, d_sub, d_cv, _, _ = make_card(grid, "DECODE  (tok/s)", (0, 0), ACCENT[0])
    _, p_big, p_sub, p_cv, _, _ = make_card(grid, "PREFILL  (tok/s)", (0, 1), ACCENT[1])
    _, m_big, m_sub, m_cv, m_qcv, m_qpc = make_card(grid, "MEMORY  (GB)", (1, 0), ACCENT[2], with_bar=True)
    _, g_big, g_sub, g_cv, _, _ = make_card(grid, "GPU  (util %)", (1, 1), ACCENT[3])

    status = tk.Label(root, text="connecting...", bg=DARK, fg=DIM, font=("Menlo", 10), anchor="w")
    status.pack(fill="x", padx=14, pady=(0, 4))

    bar = tk.Frame(root, bg=DARK)
    bar.pack(fill="x", padx=10, pady=(0, 10))
    bench_lbl = tk.Label(bar, text="", bg=DARK, fg=DIM, font=("Menlo", 10))
    bench_btn = tk.Button(bar, text="▶ Benchmark", bg="#e9e9ee", fg="#3a3a3c",
                          activebackground="#dcdce2", activeforeground="#1d1d1f",
                          relief="flat", bd=0, highlightthickness=0, cursor="hand2",
                          font=("Menlo", 10), padx=12, pady=1)
    bench_btn.pack(side="left")
    bench_lbl.pack(side="left", padx=10)

    def on_bench():
        c.try_start_bench()  # 锁内 check-and-set, 重复点击直接忽略

    bench_btn.config(command=on_bench)

    def refresh():
        s = c.snapshot()
        met_ok = fresh(s.get("met_ts"))
        if s["status"] != "ok" or not met_ok:
            status.config(text=f"⚠ {s['status']}", fg=WARN if s["status"] == "ok" else BAD)
        else:
            restart_note = ""
            if s.get("restarted_at") and time.time() - s["restarted_at"] < 30:
                restart_note = "  [检测到引擎重启, 计数已重置]"
            st = s.get("engine_status") or {}
            status.config(
                text=f"● {base}  ctx {fmt(st.get('max_ctx'), '{:.0f}')}  "
                     f"队列 {fmt(s.get('queued'), '{:.0f}')}  {'执行中' if s.get('running') else '空闲'}  "
                     f"{s.get('thermal','?')}  {s['ts']}{restart_note}", fg=OK)

            # Decode: 大字=实时(官方计数器差分), 小字只留 Benchmark
            d = s.get("decode_tps")
            d_big.config(text=fmt(d) if d is not None else ("0.0" if s.get("decode_valid") else "--"))
            d_sub.config(text=f"Bench {fmt(s.get('bench_decode_tps'))} [{s.get('bench_decode_mode') or '未测'}]")
            draw_line(d_cv, s["hist_decode"], ACCENT[0])

            # Prefill: 整批计数在结束时一次性入账, 进行中显示"···",
            # 入账后保持显示该批速率 6 秒
            pf_bench = f"Benchmark {fmt(s.get('bench_prefill_tps'), '{:.0f}')}"
            if s.get("prefill_tps"):
                p_big.config(text=fmt(s["prefill_tps"], "{:.0f}"))
                p_sub.config(text=pf_bench)
            elif s.get("prefill_prog"):
                p_big.config(text="···")
                p_sub.config(text=pf_bench)
            elif s.get("prefill_last") and fresh(s.get("prefill_last_ts"), 6):
                p_big.config(text=fmt(s["prefill_last"], "{:.0f}"))
                p_sub.config(text=pf_bench)
            else:
                p_big.config(text="0" if s.get("prefill_valid") else "--")
                p_sub.config(text=pf_bench)
            draw_line(p_cv, s["hist_prefill"], ACCENT[1], vmax=4000)

            # Memory: 大字=引擎当前/上限; 面积图=系统内存占用趋势(柔和绿);
            # 进度条=Splash 引擎配额占用, 与系统内存压力是两回事
            cur, lim = s.get("mem_cur"), s.get("mem_lim")
            peak = s.get("mem_peak")
            m_big.config(text=(fmt(cur / 2**30) if cur is not None else "--")
                         + f" / {fmt(lim / 2**30, '{:.0f}') if lim is not None else '--'}")
            used, total, _ = s.get("sysmem") or (None, None, None)
            m_sub.config(text=f"占用峰值 {fmt(peak/2**30 if peak is not None else None)} GB"
                              f"  总占用 {fmt(used)}/{fmt(total,'{:.0f}')} GB")
            draw_area(m_cv, s["hist_sysmem"], G_EDGE, G_FILL, vmax=1.0)
            m_qcv.delete("all")
            qw, qh = m_qcv.winfo_width(), max(m_qcv.winfo_height(), 10)
            if qw >= 20 and cur and lim:
                round_rect(m_qcv, 1, 1, qw - 2, qh - 2, 5, "#e9e9ee", "#e9e9ee")
                frac = min(cur / lim, 1.0)
                fw = (qw - 2) * frac
                if fw > 1:
                    round_rect(m_qcv, 1, 1, 1 + fw, qh - 2, min(5, fw / 2), TEAL, TEAL)
                m_qpc.config(text=f"{frac:.0%}")
            else:
                m_qpc.config(text="--")

            # GPU: 系统级(整机合计); 细折线, ioreg 缺数据时断线
            g = s.get("gpu") or {}
            g_big.config(text=fmt(g.get("device"), "{:.0f}") + " %")
            g_sub.config(text="")
            draw_line(g_cv, s["hist_gpu"], ACCENT[3], vmax=100)

        # Benchmark 状态栏与引擎在线/离线无关, 每拍渲染: 断线时的压测
        # 失败/进行中状态也必须可见, 不被在线分支跳过(见 bench_display)。
        btn, lbl = bench_display(s)
        bench_btn.config(text=btn, fg=DIM if s.get("bench_running") else TXT)
        bench_lbl.config(text=lbl)
        root.after(1000, refresh)

    root.after(1000, refresh)
    root.mainloop()
    c._stop.set()
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=None,
                    help="Splash endpoint, e.g. http://127.0.0.1:8000 (default)")
    ap.add_argument("--port", type=int, default=None,
                    help="shorthand for --url http://127.0.0.1:PORT")
    ap.add_argument("--cli", action="store_true", help="print one snapshot and exit")
    a, _unknown = ap.parse_known_args()  # .app 双击启动时 macOS 会塞入 -psn_xxx, 忽略即可
    base = make_base(a.port, a.url)
    return cli_mode(base) if a.cli else gui_mode(base)


if __name__ == "__main__":
    sys.exit(main())
