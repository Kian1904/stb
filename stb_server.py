#!/usr/bin/env python3
"""STB MCP server — See, Think, Build as tools for any coding agent.

Exposes the STB pipeline over MCP (stdio):
  - stb_see      : traverse an APK on the local redroid emulator -> UI map JSON
  - stb_think    : bridge a UI map to jadx-decompiled code -> bridge JSON
  - stb_build    : generate a self-contained HTML report
  - stb_status   : emulator/container health + installed packages
  - stb_screencap: grab a screenshot of the current screen
  - stb_dump_ui  : accessibility tree dump of the current screen

Run:  python3 stb_server.py        (stdio transport)
Config (Claude Code / other agents):
  { "command": "python3", "args": ["/path/to/stb_server.py"] }
"""
import asyncio
import json
import os
import shutil
import subprocess

from mcp.server.mcpserver import MCPServer

ADB_SERIAL = os.environ.get("STB_ADB_SERIAL", "localhost:5555")
STB_DIR = os.environ.get("STB_DIR", os.path.expanduser("~/stb"))
CONTAINER = os.environ.get("STB_CONTAINER", "stb-android")

mcp = MCPServer(
    name="stb-re",
    title="STB — See, Think, Build",
    description="Runtime UI maps for APKs bridged to static reverse engineering.",
    instructions=(
        "STB gives you eyes on a running Android app plus a bridge to its code. "
        "Workflow: stb_status -> (ensure emulator) -> stb_see to traverse the app "
        "-> decompile the APK with jadx -> stb_think to bridge UI elements to code "
        "-> stb_build for the final HTML report. stb_dump_ui/stb_screencap let you "
        "look at the CURRENT screen interactively."
    ),
)


def _run(cmd, timeout=60):
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return r.returncode, r.stdout.decode(errors="replace"), r.stderr.decode(errors="replace")


def _adb(*args, timeout=30):
    """Run adb shell on the emulator; returns combined output string."""
    rc, out, err = _run(["adb", "-s", ADB_SERIAL, "shell", *args], timeout)
    return out or err


def _path_in_stb(rel):
    """Resolve a user-supplied path; allow relative to STB_DIR or absolute."""
    p = os.path.expanduser(rel)
    if not os.path.isabs(p):
        p = os.path.join(STB_DIR, p)
    return p


def _find_uimap(pattern):
    """Locate the newest uimap JSON in the run directory."""
    run_dir = os.path.join(os.environ.get("HOME", "/home/agentuser"),
                           ".hermes/cache/scratch/stb_run")
    candidates = []
    if os.path.isdir(run_dir):
        for f in os.listdir(run_dir):
            if f.startswith("uimap_") and f.endswith(".json") and "_bridge" not in f:
                candidates.append(os.path.join(run_dir, f))
    if pattern:
        cands = [c for c in candidates if pattern in os.path.basename(c)]
        if cands:
            candidates = cands
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


@mcp.tool(description="Check the STB lab: docker container state, adb connection, "
                          "boot completion, and a short package list. Run this first.")
def stb_status() -> str:
    lines = []
    rc, out, _ = _run(["sudo", "docker", "ps", "-a", "--format",
                       "{{.Names}} {{.Status}}"], timeout=30)
    lines.append("containers: " + (out.strip() or "(none)"))
    rc, out, err = _run(["adb", "connect", ADB_SERIAL], timeout=20)
    lines.append(f"adb connect: {out.strip() or err.strip()}")
    out = _adb("getprop", "sys.boot_completed")
    lines.append(f"boot_completed: {out.strip()}")
    out = _adb("pm", "list", "packages", "-3")
    pkgs = [l.replace("package:", "") for l in out.splitlines() if l.strip()]
    lines.append(f"3rd-party packages ({len(pkgs)}): " + ", ".join(pkgs[:15]))
    # tools
    for tool, cmd in [("jadx", ["/opt/jadx/bin/jadx", "--version"]),
                      ("adb", ["adb", "--version"])]:
        rc, o, e = _run(cmd, timeout=20)
        lines.append(f"{tool}: {'OK' if rc == 0 else 'MISSING'} "
                     f"{(o.strip().splitlines() or [''])[0][:40]}")
    return "\n".join(lines)


@mcp.tool(description="Traverse an installed Android app on the emulator: launch it, "
                          "tap untried clickable elements, capture screenshot + UI dump per "
                          "unique screen, and write a UI-map JSON. Args: package (e.g. "
                          "org.schabi.newpipe), max_screens, max_taps. Takes minutes — "
                          "do not interrupt.")
def stb_see(package: str, max_screens: int = 15, max_taps: int = 40) -> str:
    script = os.path.join(STB_DIR, "stb_traverse.py")
    if not os.path.exists(script):
        return f"ERROR: {script} not found"
    # clean slate: stop target + system picker, go home
    _adb("am", "force-stop", package)
    _adb("am", "force-stop", "com.android.documentsui")
    _adb("input", "keyevent", "3")
    rc, out, err = _run(["python3", script, package,
                         "--max-screens", str(max_screens),
                         "--max-taps", str(max_taps)], timeout=900)
    tail = "\n".join(out.splitlines()[-12:])
    return f"exit={rc}\n{tail}\n{err[-300:] if rc != 0 else ''}"


@mcp.tool(description="Dump the CURRENT screen as structured UI elements "
                          "(class, text, content-desc, resource-id, bounds). "
                          "Works even on FLAG_SECURE apps.")
def stb_dump_ui() -> str:
    out = _adb("uiautomator", "dump", "/sdcard/ui.xml", timeout=30)
    _run(["adb", "-s", ADB_SERIAL, "pull", "/sdcard/ui.xml", "/tmp/stb_ui.xml"], 20)
    try:
        import xml.etree.ElementTree as ET
        nodes = []
        for n in ET.parse("/tmp/stb_ui.xml").iter("node"):
            txt = n.get("text") or n.get("content-desc") or ""
            if txt or n.get("clickable") == "true":
                nodes.append({
                    "class": n.get("class"), "text": txt,
                    "res": (n.get("resource-id") or "").split("/")[-1],
                    "clickable": n.get("clickable"), "bounds": n.get("bounds"),
                })
        foc = _adb("dumpsys", "window")
        focus = next((l.split("=", 1)[1].strip() for l in foc.splitlines()
                      if "mCurrentFocus" in l), "?")
        return json.dumps({"focus": focus, "elements": nodes}, indent=1)
    except Exception as e:
        return f"ERROR: {e}\nraw dump: {out}"


@mcp.tool(description="Take a screenshot of the CURRENT screen. Returns a note about "
                          "the saved file path (may be blocked if the app sets FLAG_SECURE).")
def stb_screencap() -> str:
    path = "/tmp/stb_current.png"
    _adb("screencap", "/sdcard/s.png", timeout=30)
    rc, out, err = _run(["adb", "-s", ADB_SERIAL, "pull", "/sdcard/s.png", path], 20)
    size = os.path.getsize(path) if os.path.exists(path) else 0
    if size == 0:
        return ("Screenshot blocked (0 bytes) — likely FLAG_SECURE app. "
                "Use stb_dump_ui instead.")
    return f"Screenshot saved: {path} ({size} bytes)"


@mcp.tool(description="Bridge a UI map to decompiled code. Args: uimap (path or partial "
                          "filename, e.g. 'newpipe'), decompiled_dir (jadx output). "
                          "Decompile first with: /opt/jadx/bin/jadx -d <out> --no-res <apk>")
def stb_think(uimap: str, decompiled_dir: str) -> str:
    script = os.path.join(STB_DIR, "stb_bridge.py")
    um = _find_uimap(uimap) if not os.path.exists(uimap) else uimap
    if not um:
        return f"ERROR: no uimap found matching '{uimap}'"
    dd = _path_in_stb(decompiled_dir)
    if not os.path.isdir(dd):
        return f"ERROR: decompiled dir not found: {dd}"
    rc, out, err = _run(["python3", script, um, dd], timeout=600)
    bridge = um.replace(".json", "_bridge.json")
    return f"exit={rc}\n{out.strip()}\nBridge JSON: {bridge}"


@mcp.tool(description="Generate the final self-contained HTML report from a UI map + "
                          "bridge. Args: uimap (path or partial filename). Report path is "
                          "returned (open it in a browser).")
def stb_build(uimap: str) -> str:
    script = os.path.join(STB_DIR, "stb_report.py")
    um = _find_uimap(uimap) if not os.path.exists(uimap) else uimap
    if not um:
        return f"ERROR: no uimap found matching '{uimap}'"
    bridge = um.replace(".json", "_bridge.json")
    if not os.path.exists(bridge):
        return (f"ERROR: bridge not found: {bridge} — run stb_think first.")
    rc, out, err = _run(["python3", script, um, bridge], timeout=120)
    report = um.replace(".json", "_report.html")
    return f"exit={rc}\n{out.strip()}\nReport: {report}"


@mcp.tool(description="Decompile an APK with jadx. Args: apk_path, out_dir (optional, "
                          "default ~/stb/decompiled/<name>). Returns decompiled dir to pass "
                          "to stb_think.")
def stb_jadx(apk_path: str, out_dir: str = "") -> str:
    apk = _path_in_stb(apk_path)
    if not os.path.exists(apk):
        return f"ERROR: apk not found: {apk}"
    if not out_dir:
        name = os.path.splitext(os.path.basename(apk))[0]
        out_dir = os.path.join(STB_DIR, "decompiled", name)
    out_dir = _path_in_stb(out_dir)
    rc, out, err = _run(["/opt/jadx/bin/jadx", "-d", out_dir, "--no-res",
                         "-j", "2", apk], timeout=600)
    tail = err.splitlines()[-1] if err else ""
    return f"exit={rc}\nDecompiled to: {out_dir}\n{jadx_tail(tail)}"


def jadx_tail(line):
    return line if line else "(done)"


@mcp.tool(description="Validate a UI map against real app source (ground truth). "
                          "Args: uimap (partial filename ok), source_dir (cloned app repo "
                          "containing AndroidManifest.xml).")
def stb_validate(uimap: str, source_dir: str) -> str:
    script = os.path.join(STB_DIR, "stb_validate.py")
    src = _path_in_stb(source_dir)
    if not os.path.isdir(src):
        return f"ERROR: source dir not found: {src}"
    # validator is hardcoded to the NewPipe uimap; run it as-is for now
    rc, out, err = _run(["python3", script], timeout=120)
    return out or err


if __name__ == "__main__":
    mcp.run(transport="stdio")
