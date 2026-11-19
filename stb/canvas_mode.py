#!/usr/bin/env python3
"""STB canvas mode v0.3 — screenshot-diff navigation for Flutter/canvas apps.

Some apps (Flutter, libGDX, Unity, custom canvas views) expose *zero*
interactive accessibility nodes: uiautomator/AT-SPI see one empty View and
that's it. For those, STB navigates by tapping a grid of screen coordinates,
screenshoting before/after each tap, and comparing consecutive frames with
PIL pixel-diff. A tap that changes the screen registers a new screen.

Output is a uimap JSON compatible with the rest of the STB pipeline
(bridge -> appmap -> clone-plan -> scaffold -> verify). Canvas elements get
role "canvas_tap", name "canvas:N", and pos = [x, y].

Usage:
    python3 stb/canvas_mode.py <package> [--max-taps N] [--out uimap.json]
    python3 stb/canvas_mode.py --app xfce4-mouse-settings --max-taps 20   (native GTK fallback, via xdotool)

Environment:
    DISPLAY=:1          needed for native-GTK apps under Xvfb
    STB_ADB_SERIAL      adb target (default: localhost:5555 / auto)
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time

from PIL import Image, ImageChops, ImageStat

ADB_SERIAL = os.environ.get("STB_ADB_SERIAL", "localhost:5555")
GRID_COLS = 8
GRID_ROWS = 6
DIFF_THRESHOLD = 400.0   # mean abs pixel difference to count as "screen changed"
TAP_SETTLE = 0.8         # seconds to wait after a tap before screenshoting


def _run(cmd, timeout=30):
    return subprocess.run(cmd, capture_output=True, timeout=timeout,
                          text=True, errors="replace")


def _adb(*args, timeout=30):
    serial = ADB_SERIAL
    out = _run(["adb", "-s", serial, "shell", *args], timeout)
    return out.stdout or out.stderr


def _adb_exec_out(*args, timeout=30):
    """Run adb exec-out (raw bytes, no shell escaping issues)."""
    return subprocess.run(["adb", "-s", ADB_SERIAL, "exec-out", *args],
                          capture_output=True, timeout=timeout)


def screenshot_png():
    """Return current screen as PIL Image via adb screencap -> pull."""
    tmp_remote = "/sdcard/stb_screencap.png"
    _adb("screencap", tmp_remote, timeout=30)
    local = os.path.join(tempfile.gettempdir(), "stb_screencap.png")
    r = _run(["adb", "-s", ADB_SERIAL, "pull", tmp_remote, local], 20)
    if r.returncode != 0 or not os.path.exists(local) or os.path.getsize(local) == 0:
        raise RuntimeError(f"screencap pull failed: {r.stderr[:200]}")
    return Image.open(local).convert("RGB")


def diff_score(a, b):
    """Mean absolute pixel difference between two same-size images (0..255)."""
    if a.size != b.size:
        return 1e9  # different size = definitely changed
    diff = ImageChops.difference(a, b)
    stat = ImageStat.Stat(diff)
    return sum(stat.mean) / 3.0


def tap(x, y):
    _adb("input", "tap", str(x), str(y))


def launch(package):
    """Launch an Android package via monkey-ish am start (resolve launcher first)."""
    out = _adb("cmd", "package", "resolve-activity",
               "--brief", "-c", "android.intent.category.LAUNCHER", package)
    activity = None
    for line in out.splitlines():
        if package in line and "/" in line:
            activity = line.split()[-1]
            break
    if not activity:
        activity = f"{package}/.{package.split('.')[-1]}"
    _adb("am", "start", "-n", activity)
    time.sleep(2.0)


def grid_points(w, h):
    pts = []
    for gy in range(GRID_ROWS):
        y = int(h * (gy + 0.5) / GRID_ROWS)
        for gx in range(GRID_COLS):
            x = int(w * (gx + 0.5) / GRID_COLS)
            pts.append((x, y))
    return pts


def sig_of(elements):
    blob = "|".join(f"{e['role']}:{e['name']}:{e['pos'][0]},{e['pos'][1]}"
                    for e in elements)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def canvas_traverse(package, max_taps):
    """Traverse an Android canvas app via screenshot-diff. Returns uimap dict."""
    launch(package)
    before = screenshot_png()
    w, h = before.size
    pts = grid_points(w, h)

    screens = []
    tried = set()
    taps = 0
    screen_idx = 0

    def capture_and_register(title_prefix):
        nonlocal screen_idx
        img = screenshot_png()
        els = [{"role": "canvas_tap",
                "name": f"{title_prefix} screen",
                "pos": [w // 2, h // 2]}]
        sig = sig_of(els)
        screen = {
            "sig": sig,
            "title": title_prefix,
            "elements": els,
            "screenshot": None,  # caller may embed separately
        }
        screens.append(screen)
        screen_idx += 1
        return sig, img

    cur_sig, cur_img = capture_and_register(f"{package} screen 0")

    i = 0
    while taps < max_taps and i < len(pts):
        x, y = pts[i]
        i += 1
        key = f"{x},{y}"
        if key in tried:
            continue
        tried.add(key)
        tap(x, y)
        taps += 1
        time.sleep(TAP_SETTLE)
        after = screenshot_png()
        score = diff_score(cur_img, after)
        if score > DIFF_THRESHOLD:
            # new screen
            new_sig, cur_img = capture_and_register(f"{package} screen {len(screens)}")
            els = [{"role": "canvas_tap", "name": f"canvas@{x},{y}",
                    "pos": [x, y]}]
            # mark this tap as interactive on the NEW screen
            screens[-1]["elements"] = els
            cur_sig = new_sig
        # else: no change, keep cur_img
    return {
        "package": package,
        "platform": "android-canvas",
        "screen_count": len(screens),
        "taps_performed": taps,
        "screens": screens,
        "generated": __import__("datetime").datetime.utcnow().isoformat() + "Z",
    }


def native_traverse(app_hint, max_taps):
    """Fallback for native GTK/X11 apps: xdotool tap grid + scrot."""
    pts = []
    # screen size from xdotool
    r = _run(["xdotool", "getdisplaygeometry"])
    w = h = 0
    for line in r.stdout.splitlines():
        if line.startswith("dimension:"):
            w, h = (int(v) for v in line.split()[1].split("x"))
            break
    if not w:
        w, h = 1440, 900
    pts = grid_points(w, h)

    screens = []
    tried = set()
    taps = 0

    def active_title():
        r = _run(["xdotool", "getactivewindow", "getwindowname"])
        return r.stdout.strip() or "unknown"

    cur_title = active_title()
    screens = [{"sig": "native-0", "title": cur_title,
                "elements": [{"role": "canvas_tap", "name": "root",
                              "pos": [w // 2, h // 2]}]}]
    i = 0
    while taps < max_taps and i < len(pts):
        x, y = pts[i]
        i += 1
        key = f"{x},{y}"
        if key in tried:
            continue
        tried.add(key)
        _run(["xdotool", "mousemove", str(x), str(y), "click", "1"])
        taps += 1
        time.sleep(TAP_SETTLE)
        new_title = active_title()
        if new_title != cur_title:
            screens.append({"sig": f"native-{len(screens)}",
                            "title": new_title,
                            "elements": [{"role": "canvas_tap",
                                          "name": f"canvas@{x},{y}",
                                          "pos": [x, y]}]})
            cur_title = new_title
    return {
        "package": app_hint,
        "platform": "native-canvas",
        "screen_count": len(screens),
        "taps_performed": taps,
        "screens": screens,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="STB canvas mode v0.3 — screenshot-diff navigation")
    ap.add_argument("target", help="Android package name OR native app hint")
    ap.add_argument("--max-taps", type=int, default=60)
    ap.add_argument("--out", required=True, help="uimap JSON output path")
    ap.add_argument("--app", action="store_true",
                    help="treat target as a native GTK app hint (xdotool path)")
    args = ap.parse_args(argv)

    if args.app:
        result = native_traverse(args.target, args.max_taps)
    else:
        result = canvas_traverse(args.target, args.max_taps)

    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)
    print(f"canvas mode done: {result['screen_count']} screens, "
          f"{result['taps_performed']} taps -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
