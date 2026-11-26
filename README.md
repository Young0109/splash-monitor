# Splash Monitor

A lightweight, real-time performance dashboard for the **Splash** local LLM
inference engine on Apple Silicon. Four cards, live trend graphs, one click
benchmark — and nothing else.

> **Independent, unofficial tool.** Splash Monitor is not an official Splash
> component and is not affiliated with or endorsed by the Splash authors.

![status: macOS only](https://img.shields.io/badge/platform-macOS%20(Apple%20Silicon)-lightgrey)
![license: MIT](https://img.shields.io/badge/license-MIT-blue)
![deps: stdlib only](https://img.shields.io/badge/runtime%20deps-none%20(standard%20library)-green)

<!-- SCREENSHOT PLACEHOLDER: add docs/screenshot.png (the approved UI: four
     cards — Decode / Prefill / Memory / GPU — on a light theme), then
     uncomment:                                                     -->
<!-- ![Splash Monitor screenshot](docs/screenshot.png) -->

*(Screenshot coming once the UI snapshot is approved for publishing.)*

## Features

- **Four live cards** — Decode tok/s, Prefill tok/s, Memory, GPU — with
  Activity-Monitor-style trend graphs (90-sample window, no smoothing).
- **Official metrics first.** Rates and latencies come from the engine's own
  `/metrics` counters wherever possible; anything estimated is explicitly
  labeled, and anything unavailable is shown as `--` — never a fake zero.
- **One-click Benchmark** measures Prefill (PP), Decode (TG) and TTFT using
  official stage-delta counters, with an end-to-end fallback and automatic
  concurrency-pollution detection.
- **Engine quota bar** and system memory trend on the Memory card, clearly
  separating the engine's self-reported budget from OS-level usage.
- **Zero runtime dependencies** — Python standard library + Tkinter only.
  No telemetry, no network access beyond your configured Splash endpoint.
- Runs headless too: `python3 splash_monitor.py --cli` prints one snapshot.

## Requirements

| | |
|---|---|
| Hardware | Apple Silicon Mac (M-series). |
| OS | macOS 14+ (developed and tested on a recent macOS release on an M5 Max). The UI also runs on any macOS with a modern Tk; system collectors are macOS-specific by design. |
| Splash | Running locally with its HTTP API. Tested against the API's `/metrics` and `/status` endpoints, default `http://127.0.0.1:8000`. |
| Python | 3.9+ with **Tcl/Tk ≥ 8.6.8 or 9.x** for the GUI (see [Troubleshooting](#troubleshooting)). The macOS system `/usr/bin/python3` ships legacy Tk 8.5 — fine for `--cli`, **not suitable for building the .app**. |

No admin rights are required. The app makes no outbound network requests
other than to your configured Splash endpoint (localhost by default).

## Run from source

```bash
git clone <repo-url> && cd splash-monitor
python3 splash_monitor.py                 # GUI
python3 splash_monitor.py --cli           # single snapshot, then exit
python3 splash_monitor.py --port 8080     # shorthand for --url http://127.0.0.1:8080
python3 splash_monitor.py --url http://192.0.2.10:8000   # remote engine (example IP)
```

If Splash is unreachable the monitor says so explicitly (`连不上 <endpoint>`)
and keeps retrying; it never crashes and never shows misleading zeros.

## Build the .app

```bash
PY=/usr/local/bin/python3.15 ./build_app.sh   # any Python with Tk >= 8.6.8
# → dist/Splash Monitor.app
```

The script creates an isolated venv, installs PyInstaller, generates the icon
(`tools/make_icon.py`, standard-library-only PNG encoder + `iconutil`), and
refuses to build with Tk < 8.6 (a known blank-window trap — see
Troubleshooting). The resulting bundle is self-contained: it embeds its own
Python and Tcl/Tk frameworks.

The bundle is built locally and unsigned (ad-hoc signature by PyInstaller).
If you distribute it, macOS will quarantine it on download — users must
right-click → Open once, or you can sign/notarize it yourself.

## What the numbers mean

### Decode card
- **Big number** — live rate:
  `Δ decode_output_tokens_total / Δ decode_wall_milliseconds_total`
  between samples. The denominator is the engine's *own* reported working
  time, not the sampling window: counters commit per batch, and dividing by
  wall-clock window would both under- and over-state throughput depending on
  commit timing. Official counter, labeled *实时*.
- **Bench TG** — the latest benchmark decode result, with its method label
  (*官方阶段差分* / *端到端估算* / *未测*).
- Missing counters render as `--` with a gap in the graph — a flat zero is
  only drawn when the counters exist and the engine is genuinely idle.

The CLI snapshot (`--cli`) additionally reports the official **lifetime
average** (`decode_output_tokens_total / decode_wall_milliseconds_total`,
both since engine start). Where those totals are absent, it falls back to a
value derived from `itl_p50_milliseconds` (`1000 / ITL p50`) and labels it
explicitly as *ITL p50换算参考(非平均值)* — a median-token-interval
reciprocal is a reference conversion, not an average throughput, and we
don't present it as one.

### Prefill card
Same method over the prefill counters. Prefill counters commit when a batch
finishes, so the card shows `···` while a prefill is in flight, holds the
last batch's rate for ~6 s after it lands, and shows `0` when the engine is
idle (or `--` if the counters are missing).

### TTFT
`ttft_p50/p95_milliseconds` — official engine percentiles, measured over all
requests since engine start. They appear in the **CLI snapshot**; in the GUI,
the bottom result bar shows the **per-run benchmark TTFT**, measured as the
time until the **first non-empty streamed `content` delta** (role-only chunks
don't count — that would measure header latency, not first token).

### Memory card
- **Engine** — `memory_current_bytes` / `memory_peak_bytes` /
  `memory_limit_bytes` from the engine. The teal bar is the engine's
  self-reported quota (current ÷ limit). It is deliberately teal, never red:
  a full engine budget is not a system problem.
- **System** — `vm_stat` (wired + active + compressor) over `hw.memsize`,
  free % from `memory_pressure -Q`, swap from `sysctl vm.swapusage`. The
  green area graph is system memory used over time. No system-wide pressure
  *level* is displayed: the only public API
  (`kern.memorystatus_vm_pressure_level`) reports per-process state, which is
  always "normal" — so we don't pretend otherwise.

### GPU card
`IORegistry` (`IOAccelerator`): Device/Renderer utilization % and
"GPU In use system memory". **These are system-wide counters summed across
all processes** — Apple Silicon exposes no per-process GPU counter without
root-level private APIs. The card is labeled accordingly.

### Benchmark methodology & limitations
1. Waits for an idle engine (0 queued requests, no decode activity, ~10 s).
2. **PP** — sends a long random-word prompt (no shared prefix, so the
   engine's prefix cache can't flatter the result) with `max_tokens=2`,
   then diffs the engine's prefill-stage counters.
3. **TG** — a 300-token generation, diffing decode-stage counters.
4. If the counter delta implies more tokens than the request itself
   (> 1.3×), concurrent activity is assumed and the official result is
   discarded in favor of an end-to-end estimate (labeled *端到端估算*),
   with a note shown in the UI.

Limitations: benchmarks put real load on your engine and share the machine
with everything else it is doing; single-run numbers vary run to run; the
end-to-end fallback includes HTTP and queueing overhead; results are
in-process only and reset when the monitor quits.

## Privacy & security

- No telemetry. No analytics. No external servers. The only network
  traffic is HTTP to your configured endpoint.
- All collected system stats are read from public, unprivileged interfaces
  (`vm_stat`, `sysctl`, `memory_pressure`, `ioreg`) with no admin rights.
- The Benchmark sends its own prompts to the configured endpoint — point it
  only at Splash instances you trust. Benchmark prompts are synthetic and
  contain no user content.
- The monitor does not read your model files, prompts, or chat history.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| .app window opens but is blank except the button | The bundle was built with Apple's legacy **Tk 8.5** (`/usr/bin/python3`). Its Canvas rendering is broken on recent macOS. Rebuild with `PY=<python with Tk 8.6.8+/9.x> ./build_app.sh` (python.org installer or Homebrew `python-tk`). `build_app.sh` now refuses old Tk. |
| `连不上 http://127.0.0.1:8000` | Splash isn't running or listens elsewhere. Start the engine, or pass `--url` / `--port`. |
| All official counters show `--` but engine is up | Your Splash build may not expose these `/metrics` fields. Check `curl http://127.0.0.1:8000/metrics`. |
| GPU % seems higher than "this model" | It's system-wide (all processes). That's an Apple Silicon API limit, not a bug. |
| macOS blocks the downloaded app | Right-click → Open (quarantine). Builds made on your own machine aren't affected. |
| GUI won't start on a fresh Mac | Tkinter missing: use the python.org installer or `brew install python-tk`. |

## Development

```bash
python -m unittest discover -s tests -v    # 30 unit tests, no GUI/engine needed
```

CI (GitHub Actions) runs the test suite and builds the bundle on
`macos-14` runners. Source is a single module, `splash_monitor.py`;
`tools/make_icon.py` regenerates the icon.

## Attribution

- **Splash** — the local inference engine this dashboard observes; all
  engine-side numbers come from its official counters. Splash Monitor is an
  independent third-party tool.
- **Python** and **Tkinter/Tcl-Tk** — runtime and UI toolkit.
- **PyInstaller** — bundle builds.
- Development was assisted by a locally hosted Qwen3.8-Flash-Next 125B
  model, with human-directed design, testing, and review.

## License

[MIT](LICENSE)
