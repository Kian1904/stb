#!/usr/bin/env python3
"""STB See-phase traversal: explore an Android app on any adb device,
collecting every unique screen (UI dump + screenshot) into a UI map JSON.

v0.2 features:
- BFS worklist instead of single-chain wandering: pick the screen with the
  most untried elements next, with a per-screen tap budget.
- Scroll support: when a screen has untried elements that are off-screen
  (bounds outside viewport), swipe to scroll and re-dump.
- Text-input support: elements whose label suggests input (search fields,
  EditTexts) get a tap + typed sample text instead of a blind tap.
- Navigation elements (next/continue/forward) are tried first on each screen.

Usage: python3 stb_traverse.py <package> [activity] [--max-screens N]
       [--max-taps N] [--out uimap.json] [--swipe-per-screen N]
"""
import argparse
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET


def _detect_serial():
    """Explicit STB_ADB_SERIAL wins; else first device/emulator adb sees."""
    env = os.environ.get("STB_ADB_SERIAL", "").strip()
    if env:
        return env
    adb = shutil.which("adb") or "adb"
    try:
        r = subprocess.run([adb, "devices"], capture_output=True, timeout=20)
        for line in r.stdout.decode(errors="replace").splitlines()[1:]:
            line = line.strip()
            if "\t" in line:
                serial, state = line.split("\t", 1)
                if state.strip() in ("device", "emulator"):
                    return serial
    except Exception:
        pass
    return "localhost:5555"


SERIAL = _detect_serial()
WORK = os.environ.get("STB_RUN_DIR",
                      os.path.expanduser("~/.hermes/cache/scratch/stb_run"))
ADB_BIN = shutil.which("adb") or "adb"
SCREEN_W, SCREEN_H = 720, 1280   # updated after first dump


def adb(*args, timeout=30):
    cmd = [ADB_BIN, "-s", SERIAL] + list(args)
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return r.stdout.decode(errors="replace"), r.stderr.decode(errors="replace")


def tap(x, y):
    adb("shell", "input", "tap", str(int(x)), str(int(y)))


def swipe(x1, y1, x2, y2, ms=400):
    adb("shell", "input", "swipe", str(int(x1)), str(int(y1)),
        str(int(x2)), str(int(y2)), str(ms))


def dump_ui(path):
    adb("shell", "uiautomator", "dump", "/sdcard/ui.xml", timeout=30)
    out, _ = adb("pull", "/sdcard/ui.xml", path, timeout=30)
    return os.path.exists(path) and os.path.getsize(path) > 0


def screencap(path):
    adb("shell", "screencap", "/sdcard/s.png", timeout=30)
    adb("pull", "/sdcard/s.png", path, timeout=30)
    if os.path.exists(path):
        return os.path.getsize(path)
    return 0


def parse_nodes(xml_path):
    """All interesting nodes: clickable ones AND text-input fields,
    with parsed bounds so we can tell on-screen vs off-screen."""
    global SCREEN_W, SCREEN_H
    nodes = []
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError:
        return nodes
    root = tree.getroot()
    rb = root.get("bounds", "")
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", rb)
    if m:
        SCREEN_W, SCREEN_H = int(m.group(3)), int(m.group(4))
    for n in tree.iter("node"):
        bounds = n.get("bounds", "")
        mb = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
        if not mb:
            continue
        l, t, r, b = (int(g) for g in mb.groups())
        clickable = n.get("clickable") == "true"
        is_edit = "EditText" in (n.get("class") or "")
        # menu/drawer rows often report clickable=false on the label node
        # (the parent handles the tap) — include them if they have a label
        has_label = bool(n.get("text") or n.get("content-desc"))
        if not (clickable or is_edit or has_label):
            continue
        w = r - l
        h = b - t
        if w <= 0 or h <= 0:
            continue
        nodes.append({
            "class": n.get("class"),
            "text": n.get("text") or "",
            "desc": n.get("content-desc") or "",
            "res": (n.get("resource-id") or "").split("/")[-1],
            "x": (l + r) // 2,
            "y": (t + b) // 2,
            "w": w, "h": h,
            "clickable": clickable,
            "editable": is_edit,
            "onscreen": 0 <= (t + b) // 2 < SCREEN_H - 50,
        })
    return nodes


def screen_signature(xml_path):
    """Signature = focused window component + sorted text content hash."""
    out, _ = adb("shell", "dumpsys", "window", timeout=15)
    focus = "?"
    for line in out.splitlines():
        if "mCurrentFocus" in line:
            focus = line.split("=", 1)[1].strip()
            break
    if focus == "?":
        out2, _ = adb("shell", "dumpsys", "activity", "activities", timeout=15)
        for line in out2.splitlines():
            if "mFocusedWindow" in line or "ResumedActivity" in line:
                focus = line.split(":", 1)[-1].strip()[:120]
                break
    if focus.startswith("Window{") and "u0 " in focus:
        focus = focus.split("u0 ")[-1].rstrip("}")
    texts = []
    try:
        for n in ET.parse(xml_path).iter("node"):
            t = n.get("text") or n.get("content-desc")
            if t:
                texts.append(t)
    except ET.ParseError:
        pass
    sig = focus + "|" + hashlib.sha1(
        "|".join(sorted(texts)).encode()).hexdigest()[:12]
    return sig, focus


def launch_app(pkg, activity=None):
    adb("shell", "am", "force-stop", pkg)
    time.sleep(1)
    if activity is None:
        out, _ = adb("shell", "cmd", "package", "resolve-activity", "--brief",
                     "-c", "android.intent.category.LAUNCHER", pkg, timeout=15)
        lines = [l for l in out.splitlines() if "/" in l and "package" not in l]
        activity = lines[-1].split("/", 1)[1].strip() if lines else None
    if activity:
        out, _ = adb("shell", "am", "start", "-n", f"{pkg}/{activity}")
        if "Error" in out or "Exception" in out:
            adb("shell", "monkey", "-p", pkg, "-c",
                "android.intent.category.LAUNCHER", "1", timeout=15)
    time.sleep(5)


def current_focus_component():
    out, _ = adb("shell", "dumpsys", "window", timeout=15)
    for line in out.splitlines():
        if "mCurrentFocus" in line:
            f = line.split("=", 1)[1].strip()
            if "u0 " in f:
                return f.split("u0 ")[-1].rstrip("}")
            return f
    return "?"


def element_key(e):
    return f"{e['x']},{e['y']}"


def nav_rank(e):
    lbl = ((e["text"] or "") + (e["desc"] or "") + e["res"]).lower()
    if e.get("editable"):
        return 3          # inputs after plain buttons
    # really-clickable nodes first; label-only rows (drawer/menu items whose
    # parent handles the tap) after them
    base = 0 if e.get("clickable") else 1
    if re.search(r"\b(next|forward|continue|get started|done|agree)\b", lbl):
        return base
    if re.search(r"\b(previous|back)\b", lbl):
        return 2 + base
    if e.get("editable"):
        return 3 + base
    return 1 + base


def is_system_trigger(e):
    label = (e["text"] or e["desc"] or "").upper()
    return any(s in label for s in ("IMPORT", "OPEN DOCUMENT", "BROWSE",
                                    "OPEN FILE", "CHOOSE FILE"))


INPUT_SAMPLES = ["stb test", "hello", "abc", "123"]


def type_into(e):
    """Tap an editable element and type a short sample text."""
    tap(e["x"], e["y"])
    time.sleep(1.5)
    text = random.choice(INPUT_SAMPLES)
    adb("shell", "input", "text", text)
    time.sleep(1)
    return text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("package")
    ap.add_argument("activity", nargs="?", default=None)
    ap.add_argument("--max-screens", type=int, default=15)
    ap.add_argument("--max-taps", type=int, default=40)
    ap.add_argument("--swipe-per-screen", type=int, default=2,
                    help="max scrolls attempted per screen")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    os.makedirs(WORK, exist_ok=True)
    out_path = args.out or os.path.join(WORK, f"uimap_{args.package}.json")

    adb("connect", SERIAL)
    launch_app(args.package)

    screens = {}
    tried = set()          # (screen_sig, element_key)
    swipes = {}            # screen_sig -> swipes done
    taps_on_screen = {}    # screen_sig -> taps spent on it
    dead = set()           # exhausted screen sigs
    taps = 0
    back_presses = 0
    stuck = 0

    dump_path = os.path.join(WORK, "cur.xml")

    def capture_current():
        """Dump+screenshot the current screen; register it. Returns sig."""
        if not dump_ui(dump_path):
            return None
        sig, focus = screen_signature(dump_path)
        nodes = parse_nodes(dump_path)
        png_path = os.path.join(
            WORK, f"screen_{sig.replace('|', '_').replace('/', '_')[:60]}.png")
        shot = screencap(png_path)
        if sig not in screens:
            screens[sig] = {
                "id": len(screens),
                "focus": focus,
                "signature": sig,
                "screenshot": png_path if shot > 0 else None,
                "screenshot_blocked_by_flag_secure": shot == 0,
                "elements": nodes,
                "swipes_done": 0,
            }
            n_on = sum(1 for e in nodes if e["onscreen"])
            print(f"[screen {screens[sig]['id']}] {focus}  "
                  f"({len(nodes)} interactive, {n_on} on-screen, shot={shot}B)")
        return sig

    # drawer rows report clickable=false but still navigate — always try
    # labeled rows; rank system triggers first so they get marked tried
    def untried_on(sig):
        done = {k for (s, k) in tried if s == sig}
        out = [e for e in screens[sig]["elements"]
               if element_key(e) not in done
               and e["onscreen"]
               and not is_system_trigger(e)
               and e["res"] not in ("back", "home", "back_button")]
        return sorted(out, key=nav_rank)

    while taps < args.max_taps and len(screens) < args.max_screens \
            and back_presses < 25:
        sig = capture_current()
        if sig is None:
            time.sleep(2)
            continue
        focus = screens[sig]["focus"]

        # guards: launcher / system UI drift
        if "com.android.launcher" in focus:
            print("  drifted to launcher — relaunching")
            launch_app(args.package)
            back_presses += 1
            continue
        if focus.startswith("com.android.") \
                or focus.startswith("com.google.android.documentsui"):
            print("  in system UI — backing out")
            adb("shell", "input", "keyevent", "4")
            back_presses += 1
            time.sleep(2.5)
            continue

        # depth-first within the current screen...
        cur = untried_on(sig)
        if not cur:
            # ...then scroll if budget remains (drawer/list may have more)
            if screens[sig]["swipes_done"] < args.swipe_per_screen:
                screens[sig]["swipes_done"] += 1
                swipes[sig] = swipes.get(sig, 0) + 1
                print(f"  scroll {screens[sig]['swipes_done']}"
                      f"/{args.swipe_per_screen} on screen "
                      f"{screens[sig]['id']}")
                swipe(SCREEN_W // 2, int(SCREEN_H * 0.75),
                      SCREEN_W // 2, int(SCREEN_H * 0.3))
                time.sleep(2)
                continue
            # exhausted → mark dead, back out toward a screen with work left
            dead.add(sig)
            adb("shell", "input", "keyevent", "4")
            back_presses += 1
            time.sleep(2.5)
            continue

        e = cur[0]
        tried.add((sig, element_key(e)))
        label = e["text"] or e["desc"] or e["res"] or e["class"]
        if e["editable"]:
            print(f"  input ({e['x']},{e['y']}) {e['class']} '{label}'")
            typed = type_into(e)
            taps += 1
            print(f"    typed: '{typed}'")
        else:
            print(f"  tap ({e['x']},{e['y']}) {e['class']} '{label}'")
            tap(e["x"], e["y"])
            taps += 1
        back_presses = 0
        time.sleep(3)

        # proactive scroll: long lists (drawers, feeds) hide items below the
        # fold. After 6 taps on the same screen, scroll once even if untried
        # elements remain, so below-the-fold rows (Settings, About) surface.
        taps_on_screen[sig] = taps_on_screen.get(sig, 0) + 1
        if (taps_on_screen[sig] % 6 == 0
                and screens[sig]["swipes_done"] < args.swipe_per_screen):
            screens[sig]["swipes_done"] += 1
            swipes[sig] = swipes.get(sig, 0) + 1
            print(f"  proactive scroll {screens[sig]['swipes_done']}"
                  f"/{args.swipe_per_screen} after {taps_on_screen[sig]} taps")
            swipe(SCREEN_W // 2, int(SCREEN_H * 0.75),
                  SCREEN_W // 2, int(SCREEN_H * 0.3))
            time.sleep(2)

        # stuck detection: same signature after the interaction
        new_sig = capture_current() or sig
        if new_sig == sig:
            stuck += 1
            if stuck >= 2 and sig not in dead:
                dead.add(sig)
                print("  screen stuck — declared dead, back out")
                adb("shell", "input", "keyevent", "4")
                back_presses += 1
                stuck = 0
                time.sleep(2.5)
        else:
            stuck = 0

    result = {
        "package": args.package,
        "activity": args.activity,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "screen_count": len(screens),
        "taps_performed": taps,
        "back_presses": back_presses,
        "swipes_performed": sum(swipes.values()),
        "traversal": "bfs-v0.2",
        "screens": list(screens.values()),
    }
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nUI map: {out_path} — {len(screens)} screens, {taps} taps, "
          f"{sum(swipes.values())} swipes, {back_presses} backs")


if __name__ == "__main__":
    main()
