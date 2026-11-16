# STB — See, Think, Build

**Runtime UI maps for Android apps, bridged to static reverse engineering.**

STB is a three-phase pipeline that gives coding agents what pure static analysis
cannot see: what an app actually looks like when it runs — and which code is
behind every button.

```
APK ──> [SEE]        runtime traversal on a headless Android emulator
         │           screenshots + accessibility dumps of every reachable screen
         ▼
       [THINK]       bridge UI elements to decompiled code (jadx)
         │           resource-id / text / content-desc → file evidence
         ▼
       [BUILD]       self-contained HTML report: screenshot, element table,
                     code evidence per element
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

| File | Phase | What it does |
|---|---|---|
| `stb_traverse.py` | See | Auto-explores an APK on a headless Android emulator (redroid): launches the app, dumps the accessibility tree, taps untried clickable elements, captures a screenshot per unique screen, emits a UI-map JSON. |
| `stb_bridge.py` | Think | Reads the UI map, searches jadx-decompiled sources for each element (via `R.id.*`, text, content-desc), resolves the Activity class implementing each screen, emits a bridge JSON. |
| `stb_report.py` | Build | Merges UI map + bridge into a single self-contained HTML report (screenshots base64-embedded, evidence tables). |
| `stb_validate.py` | eval | Ground-truth validation: compares STB findings against the app's real manifest & source. |

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

- [ ] MCP server (`stb-re`) so any coding agent can drive the pipeline
- [ ] BFS traversal with per-activity budgets + drawer/scroll awareness
- [ ] Text-input actions during traversal
- [ ] Coverage regression tracking per APK version

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
