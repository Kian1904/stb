# STB — See, Think, Build

**Runtime UI maps for Android & native desktop apps, bridged to static reverse engineering — with a clone scaffold and a round-trip verifier.**

STB is a pipeline that gives coding agents what pure static analysis
cannot see: what an app actually looks like when it runs — and which code is
behind every button. v0.4 closes the loop: **See the original → map it →
plan the parts → scaffold a compilable clone → run it → See the clone →
score how well it matches.**

```
TARGET ──> [SEE]        runtime traversal (Android emulator / AT-SPI on X11)
            │           screenshots + accessibility dumps of every screen
            ▼
          [THINK]       bridge UI elements to code evidence
            │           Android: jadx (R.id/text/desc) · Native: source + ext-dep
            ▼
          [MAP]         appmap.json — screens + flows + states + functions
            │           one agent-digestible JSON, machine-checkable
            ▼
          [PLAN]        clone_plan.json — build parts sorted easy → hard,
            │           per-part toolchain + acceptance criteria
            ▼
          [SCAFFOLD]    compilable project (GTK3 meson/ninja · Android gradle)
            │           generated .ui / layouts from the observed UI
            ▼
          [VERIFY]      See(clone) vs original — per-part coverage + verdict
```

## Why

Static analyzers (REA, Ghidra, jadx) are blind to runtime UI: they see
`MainActivity.java` but not that the home screen has a drawer, a search bar, and
a bottom tab bar. GUI agents (AppAgent, ARTEMIS) see the UI but are task-driven —
they complete a mission, not map the app.

STB is the missing piece: **exhaustive UI documentation as an artifact**, with
every element bridged to the code that implements it. Ground-truth validated
against real app sources (see `stb_validate.py`).

## Files

| Module | Phase | What it does |
|---|---|---|
| `stb/traverse.py` | See (Android) | Auto-explores an APK on a headless emulator: dumps the accessibility tree, taps untried elements, screenshots each unique screen, emits a UI-map JSON **plus a traversal log** (`*_traversal.log`) that records every trigger → screen transition. |
| `stb/see_native.py` | See (Linux) | Same contract for desktop apps: AT-SPI tree walk, xdotool clicks, scrot screenshots, anti-thrash BFS over menus/buttons. |
| `stb/bridge.py` | Think (Android) | Bridges each UI element to decompiled source (`R.id.*`, text, content-desc) and resolves the Activity per screen. |
| `stb/bridge_native.py` | Think (Linux) | Bridges to a source tree plus on-disk external dependency indexes (gtksourceview language-specs/styles), classifying each miss honestly: `source-string` / `ext-dep` / `runtime-generated` / `unresolved`. |
| `stb/appmap.py` | Map | Merges uimap + bridge into **appmap.json**: `screens`, `flows` (navigation graph from the traversal log), `states` (variants, Jaccard > 0.8), `functions` (per-element event catalogue with evidence). |
| `stb/clone.py` | Plan | Decomposes the map into **build parts sorted easy → hard**, with difficulty scoring, dependencies, per-target toolchain, and acceptance criteria per part. |
| `stb/scaffold.py` | Build | Emits a **compilable project**: GTK3 (meson + generated `.ui` with nested menu hierarchy, one notebook page per screen) or Android (gradle + layouts + activities). |
| `stb/verify.py` | Verify | Round-trip grader: runs (or loads) a See pass over the clone and scores per-part acceptance coverage + label-set similarity, verdict `PASS` / `PARTIAL`. |
| `stb/report.py` | Build (legacy) | Self-contained HTML report with embedded screenshots and code evidence. |
| `stb/sanitize.py` | Guard | Prompt-injection scanner for untrusted artifacts (`stb-scan`). |
| `stb/validate.py` | eval | Ground-truth validation against the app's real manifest & source. |

## Clone round-trip (v0.4)

End-to-end proven on **mousepad** (Xfce text editor, source-cloned ground truth):

```bash
# 1-2. See + bridge the original (Xvfb XFCE session, AT-SPI)
stb-see-native --app mousepad --max-actions 25 --out uimap.json
stb-bridge-native uimap.json --source mousepad-src/ \
   --ext-dep /usr/share/gtksourceview-4/language-specs --out bridge.json

# 3. Map — one JSON: screens, flows, states, functions
stb-appmap uimap.json bridge.json --traversal-log uimap_traversal.log \
   --source mousepad-src/ --out appmap.json

# 4. Plan — parts easy→hard with toolchain + acceptance
stb-clone appmap.json                       # → appmap_clone_plan.json

# 5. Scaffold — compilable GTK3 project
stb-scaffold appmap.json appmap_clone_plan.json --out-dir clone/
cd clone && meson setup build && ninja -C build   # → runs on X11

# 6. Verify — See the clone, score against the original
stb-verify appmap.json appmap_clone_plan.json --see --app stb-clone
# → per-part coverage, verdict PASS/PARTIAL, missing-label lists
```

Results (mousepad): bridge 99.0% resolve (513 source / 810 ext-dep / 23
runtime-generated, 14 honest unresolved); appmap 6 screens / 6 flows / 278
functions; scaffold binary runs on X11 with the full nested menu bar; verify
correctly reports part 3 (Menus & actions) 20/20 PASS while unimplemented
logic parts score 0 — the gap a builder agent must close.

## Setup (VM, no KVM needed)

```bash
# Android 11 in Docker via redroid (binder on host kernel)
sudo modprobe binder_linux
sudo mkdir -p /dev/binderfs && sudo mount -t binder binder /dev/binderfs
docker run -d --name stb-android --privileged -v /dev/binderfs:/dev/binderfs \
  -p 5555:5555 redroid/redroid:11.0.0-latest \
  androidboot.redroid_width=720 androidboot.redroid_height=1280 \
  androidboot.redroid_dpi=320 androidboot.redroid_gpu_mode=guest
sleep 45 && adb connect localhost:5555

# tools
sudo apt-get install -y adb openjdk-17-jdk-headless
# jadx: https://github.com/skylot/jadx/releases
```

## Usage

```bash
# 1. SEE — traverse the app (a few minutes, ~40 taps)
python3 stb_traverse.py org.schabi.newpipe --max-screens 20 --max-taps 40

# 2. THINK — decompile & bridge
jadx -d decompiled/newpipe --no-res -j 2 newpipe.apk
python3 stb_bridge.py <uimap>.json decompiled/newpipe

# 3. BUILD — report
python3 stb_report.py <uimap>.json <bridge>.json
# → opens as a single HTML file, works offline
```

## Validated results (Nov 2026)

Test subjects: NewPipe 0.29.3, Aegis (open-source, F-Droid).

- NewPipe: **17 screens mapped, 471 code references** bridged
- Aegis: 4 screens; every button resolved to its exact implementing class
  (`IMPORT VAULT` → `WelcomeSlide.java` L53, radios → `SecurityPickerSlide.java`)
- Ground truth vs real NewPipe source: **all verified screens MANIFEST-OK**,
  drawer coverage 7/10 items within a 40-tap budget (9/10 with targeted deep-dive)
- `FLAG_SECURE` apps (Aegis) block screenshots but **not** accessibility dumps —
  STB detects and reports this as a security signal

## Known limitations

- Traversal is DFS with a tap budget — deep screens (Settings, About, Player)
  may need more budget or targeted navigation
- Text-based code evidence can be noisy on large apps; `R.id` evidence is precise
- Wizard screens requiring text input (e.g. password setup) are declared "dead"
  and backed out of

## Roadmap

- [x] **v0.1** MCP server (`see-think-build`) so any coding agent can drive the pipeline
- [x] **v0.2** BFS-style traversal with scroll awareness (proactive swipes on long lists/drawers)
- [x] **v0.2** Text-input actions during traversal (tap + typed sample)
- [x] **v0.2** Layout-XML bridge: res-id → declaring layout/menu XML → `R.layout.*` inflate site
- [x] **v0.2** Labeled-row tapping (drawer items report `clickable=false` but still navigate)
- [x] **v0.3** One-command dissection (`stb-dissect`), APK acquisition (F-Droid/Aptoide), bridge dedupe
- [x] **v0.3.1** Injection guard: `stb-scan` scanner, Security Notes in reports, `[DATA-ONLY]` MCP banner
- [x] **v0.4** Native desktop support: See via AT-SPI, bridge with ext-dep evidence, honest miss classes
- [x] **v0.4** `appmap.json`: unified agent-digestible map (screens/flows/states/functions)
- [x] **v0.4** Clone plan: parts easy→hard, per-part toolchain + acceptance
- [x] **v0.4** Scaffold: compilable GTK3/Android skeleton from the map (binary runs on X11)
- [x] **v0.4** Verify: round-trip grader, per-part coverage verdict
- [ ] Implement scaffold parts 2+ (real handlers) to lift verify verdict PARTIAL → PASS
- [ ] Windows/macOS See (UIA / AX API — same uimap contract)
- [ ] Blutter-class Dart snapshot parsing for richer Flutter appmaps
- [ ] Direct deep-link navigation between screens (skip back-walking)
- [ ] Coverage regression tracking per APK version

## Install

From PyPI (recommended):

```bash
pip install see-think-build
stb-server            # run the MCP server (stdio)

# CLI surface (Android pipeline + native round-trip):
stb-see org.schabi.newpipe --max-screens 15
stb-think uimap.json decompiled/
stb-build uimap.json uimap_bridge.json
stb-see-native --app mousepad --max-actions 25      # Linux AT-SPI
stb-bridge-native uimap.json --source src/ --out bridge.json
stb-appmap uimap.json bridge.json --out appmap.json
stb-clone appmap.json                               # parts easy→hard
stb-scaffold appmap.json appmap_clone_plan.json     # compilable skeleton
stb-verify appmap.json appmap_clone_plan.json --uimap clone_uimap.json
stb-scan <file>                                     # injection guard
```

From source:

```bash
git clone https://github.com/Kian1904/stb && cd stb
pip install .
```

Python 3.10+; only hard dependency is the `mcp` package. `adb` and `jadx`
are external binaries you install separately (see below).

## Cross-platform setup

STB runs on **Linux, macOS, and Windows**. It talks to anything adb can reach:
an official Android Studio emulator (AVD), a physical phone over USB, or a
redroid container (Linux/Docker only). Requirements:

- **Python 3.10+**
- **adb** (Android platform-tools) — on PATH or via `STB_ADB`
- **jadx** — on PATH or via `STB_JADX` (github.com/skylot/jadx; needs Java 17+)

Setup per OS:

| Step | Linux | macOS | Windows |
|---|---|---|---|
| adb | `sudo apt install adb` or platform-tools zip | `brew install android-platform-tools` | Download platform-tools zip, add to PATH |
| jadx | `unzip jadx-x.y.zip` → add `bin/` to PATH | same | same (`jadx.bat`) |
| Emulator | Android Studio AVD, USB phone, or redroid (Docker) | AVD or USB phone | AVD or USB phone |
| MCP config | `python3` | `python3` | `python` (or full path) |

Device targeting — set `STB_ADB_SERIAL` if more than one device is connected
(find serials with `adb devices -l`):

```bash
export STB_ADB_SERIAL=emulator-5554   # or 127.0.0.1:5555, or a USB serial
export STB_JADX=/path/to/jadx/bin/jadx
export STB_DIR=~/stb                  # where maps/reports/decompiled go
```

Run `stb_status` first — it reports exactly what is missing and how to fix it.

### Using a physical phone (Termux-friendly)

1. Enable USB debugging (Developer options), connect, accept the fingerprint
2. From Termux: `pkg install python android-tools`, clone this repo
3. `python stb_server.py` — the phone you're debugging can be a *second*
   device; the controlling agent runs anywhere adb works

## MCP server

STB also ships as an MCP server (`stb_server.py`) so any MCP-capable coding agent
(Claude Code, Codex, Hermes, …) can drive the whole pipeline:

| Tool | What it does |
|---|---|
| `stb_status` | Lab health: container, adb, boot state, packages, jadx |
| `stb_see` | Traverse an app → UI-map JSON (screens + elements + screenshots) |
| `stb_dump_ui` | Accessibility tree of the CURRENT screen (works on FLAG_SECURE apps) |
| `stb_screencap` | Screenshot of the current screen |
| `stb_jadx` | Decompile an APK (jadx) |
| `stb_think` | Bridge UI-map elements to decompiled code |
| `stb_build` | Generate the self-contained HTML report |
| `stb_validate` | Ground-truth check vs real app source |

Config (generic stdio):

```json
{
  "mcpServers": {
    "stb": { "command": "python3", "args": ["/path/to/stb_server.py"] }
  }
}
```

Env overrides: `STB_DIR`, `STB_ADB_SERIAL`, `STB_ADB`, `STB_JADX`,
`STB_RUN_DIR`, `STB_CONTAINER` (see Cross-platform setup above).

## License

MIT
