"""Command-line entry points (console_scripts) for see-think-build.

Thin wrappers that re-export the pipeline scripts' main() functions.
"""
import os
import shutil
import subprocess
import sys


def _run_script(name, args=None):
    """Run a bundled pipeline script with the current interpreter."""
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, name)
    cmd = [sys.executable, script] + (list(args) if args else sys.argv[1:])
    sys.exit(subprocess.run(cmd).returncode)


def see():
    """stb-see <package> [--max-screens N --max-taps N] — traverse an app."""
    _run_script("traverse.py")


def dissect():
    """stb-dissect <package> — one-command full flow: acquire APK,
    install, traverse, decompile+bridge, HTML report."""
    _run_script("dissect.py")


def think():
    """stb-think <uimap.json> <decompiled_dir> — bridge UI to code."""
    _run_script("bridge.py")


def build():
    """stb-build <uimap.json> <bridge.json> — generate the HTML report."""
    _run_script("report.py")


def validate():
    """stb-validate [uimap.json] [source_dir] — ground-truth check."""
    _run_script("validate.py")


def server():
    """stb-server — run the MCP server (stdio)."""
    if shutil.which("adb") is None and not os.environ.get("STB_ADB"):
        print("stb: adb not found — install Android platform-tools "
              "or set STB_ADB", file=sys.stderr)
    _run_script("server.py")


def scan():
    """stb-scan <path> — scan skills/plugins/stb artifacts for
    prompt-injection-shaped text before trusting them."""
    _run_script("sanitize.py")


def appmap():
    """stb-appmap <uimap.json> [bridge.json] [--traversal-log F] —
    unified JSON app map (screens+flows+states+functions)."""
    _run_script("appmap.py")


def clone():
    """stb-clone <appmap.json> — build-parts decomposition, easy→hard,
    per-part toolchain + acceptance."""
    _run_script("clone.py")


def scaffold():
    """stb-scaffold <appmap.json> [plan.json] — compilable project
    skeleton from the app map (GTK3 / Android)."""
    _run_script("scaffold.py")


def verify():
    """stb-verify <appmap.json> <plan.json> [--uimap F | --see --app N] —
    round-trip grader: See(clone) vs original app map."""
    _run_script("verify.py")


def see_native():
    """stb-see-native --app NAME — See phase for Linux desktop apps
    (AT-SPI/X11). Needs system python3 with pyatspi."""
    _run_script("see_native.py")


def bridge_native():
    """stb-bridge-native <uimap.json> --source DIR [--ext-dep DIR]… —
    bridge native desktop uimap to source/ext-dep evidence."""
    _run_script("bridge_native.py")


def canvas():
    """stb-canvas <target> [--max-taps N --out uimap.json] —
    screenshot-diff navigation for Flutter/canvas apps (no AT-SPI nodes)."""
    _run_script("canvas_mode.py")
