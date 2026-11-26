# Proposed initial release: v1.0.0 (not yet published)

## Suggested repository
- Name: `splash-monitor`
- Description: *Lightweight real-time dashboard for the Splash local LLM
  inference engine on Apple Silicon. Unofficial.*
- Topics: `macos`, `apple-silicon`, `llm`, `inference`, `monitoring`,
  `dashboard`, `tkinter`, `pyinstaller`, `splash`

## Release notes draft

### Splash Monitor v1.0.0

First public release.

- Four-card live dashboard (Decode / Prefill / Memory / GPU) with
  Activity-Monitor-style trend graphs.
- Official engine counters first: rates computed from `Δtokens / Δengine
  wall-clock`; estimates are explicitly labeled; unavailable values show
  `--` instead of fake zeros.
- One-click Benchmark (PP / TG / TTFT) with official stage-delta method,
  end-to-end fallback, and concurrency-pollution detection.
- Configurable endpoint: `--url` / `--port` (default
  `http://127.0.0.1:8000`).
- CLI snapshot mode: `--cli`.
- `build_app.sh` produces a self-contained `Splash Monitor.app`
  (PyInstaller; requires Tcl/Tk ≥ 8.6.8 — the script enforces it).
- 59 stdlib unit tests; CI builds and tests on macOS runners.

Known limitations:
- GPU utilization is system-wide (no public per-process API on Apple
  Silicon).
- No system-wide memory-pressure *level* is shown (no reliable public API).
- GUI verified on one M5 Max machine / recent macOS; other M-series
  configurations share the same code paths but are untested.

Distribution asset: after CI builds the bundle, package it with
macOS-native `ditto` (as CI does — never upload the raw `.app` directory,
which loses Unix exec bits and symlink semantics):

```sh
ditto -c -k --keepParent "dist/Splash Monitor.app" \
      "Splash-Monitor-v1.0.0-macos-arm64.zip"
# verify the round-trip before publishing:
ditto -x -k Splash-Monitor-v1.0.0-macos-arm64.zip /tmp/check
codesign --verify --deep --strict "/tmp/check/Splash Monitor.app"
```

Attach `Splash-Monitor-v1.0.0-macos-arm64.zip` to the GitHub Release.
Note: bundle is ad-hoc signed, not notarized.
