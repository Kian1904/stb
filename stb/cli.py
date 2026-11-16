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
