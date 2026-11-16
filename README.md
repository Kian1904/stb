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

## License

MIT
