#!/usr/bin/env python3
"""STB See-phase traversal: explore an Android app on the redroid emulator,
collecting every unique screen (UI dump + screenshot) into a UI map JSON.

Usage: python3 stb_traverse.py <package> <activity> [--max-depth N] [--out stb_uimap.json]
"""
import argparse
import hashlib
import json
import os
import subprocess
import time
import xml.etree.ElementTree as ET

SERIAL = "localhost:5555"
WORK = os.path.expanduser("~/.hermes/cache/scratch/stb_run")


def adb(*args, timeout=30):
    cmd = ["adb", "-s", SERIAL] + list(args)
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return r.stdout.decode(errors="replace"), r.stderr.decode(errors="replace")


def tap(x, y):
    adb("shell", "input", "tap", str(int(x)), str(int(y)))


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
    """Return list of interesting elements: clickable with bounds+text."""
    nodes = []
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError:
        return nodes
    for n in tree.iter("node"):
        if n.get("clickable") != "true":
            continue
        bounds = n.get("bounds", "")
        if not bounds.startswith("["):
            continue
        try:
            l, t = bounds.split("][")[0][1:].split(",")
            r, b = bounds.split("][")[1][:-1].split(",")
        except (ValueError, IndexError):
            continue
        # skip navbar/nav gestures at very bottom or system UI
        nodes.append({
            "class": n.get("class"),
            "text": n.get("text") or "",
            "desc": n.get("content-desc") or "",
            "res": (n.get("resource-id") or "").split("/")[-1],
            "x": (int(l) + int(r)) // 2,
            "y": (int(t) + int(b)) // 2,
        })
    return nodes


def screen_signature(xml_path):
    """Signature of a screen = focused window component + its text content."""
    out, _ = adb("shell", "dumpsys", "window", timeout=15)
    focus = "?"
    for line in out.splitlines():
        if "mCurrentFocus" in line:
            focus = line.split("=", 1)[1].strip()
            break
    if focus == "?":
        # dumpsys window may buffer oddly; try the activity dump instead
        out2, _ = adb("shell", "dumpsys", "activity", "activities", timeout=15)
        for line in out2.splitlines():
            if "mFocusedWindow" in line or "ResumedActivity" in line:
                focus = line.split(":", 1)[-1].strip()[:120]
                break
    # normalize: use the component part of the window token (hash changes
    # between launches; the component is the stable screen identity)
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
    sig = focus + "|" + hashlib.sha1("|".join(sorted(texts)).encode()).hexdigest()[:12]
    return sig, focus


def launch_app(pkg, activity=None):
    """Launch app via its exported launcher activity (resolve-activity)."""
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
            # fall back to monkey launcher intent
            adb("shell", "monkey", "-p", pkg, "-c",
                "android.intent.category.LAUNCHER", "1", timeout=15)
    time.sleep(5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("package")
    ap.add_argument("activity", nargs="?", default=None)
    ap.add_argument("--max-screens", type=int, default=15)
    ap.add_argument("--max-taps", type=int, default=40)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    os.makedirs(WORK, exist_ok=True)
    out_path = args.out or os.path.join(WORK, f"uimap_{args.package}.json")

    adb("connect", SERIAL)
    launch_app(args.package)  # resolve exported launcher activity

    screens = {}          # sig -> screen record
    tried = set()         # (screen_sig, element_key) already tapped
    dead = set()          # screen sigs we gave up on (no progress possible)
    stuck = 0             # consecutive taps that did not change the screen
    last_sig = None
    taps = 0
    back_presses = 0

    while taps < args.max_taps and len(screens) < args.max_screens and back_presses < 25:
        # if the current focus is a *system* picker/dialog spawned by a tap,
        # prefer backing out of it instead of exploring system apps
        _, focus_out = adb("shell", "dumpsys", "window", timeout=15)
        cur_focus = "?"
        for line in focus_out.splitlines():
            if "mCurrentFocus" in line:
                cur_focus = line.split("=", 1)[1].strip()
                break
        if cur_focus != "?" and not cur_focus.startswith("Window{?"):
            focus_component = cur_focus.split("u0 ")[-1].rstrip("}") if "u0 " in cur_focus else cur_focus
        else:
            focus_component = cur_focus
        is_system_component = focus_component.startswith(("com.android.launcher",
            "com.android.systemui"))
        dump_path = os.path.join(WORK, "cur.xml")
        if not dump_ui(dump_path):
            time.sleep(2)
            continue
        sig, focus = screen_signature(dump_path)
        png_path = os.path.join(WORK, f"screen_{sig.replace('|', '_').replace('/', '_')[:60]}.png")
        shot_bytes = screencap(png_path)

        if sig not in screens:
            screens[sig] = {
                "id": len(screens),
                "focus": focus,
                "signature": sig,
                "screenshot": png_path if shot_bytes > 0 else None,
                "screenshot_blocked_by_flag_secure": shot_bytes == 0,
                "elements": [
                    {"class": e["class"], "text": e["text"], "desc": e["desc"],
                     "res": e["res"], "x": e["x"], "y": e["y"]}
                    for e in parse_nodes(dump_path)
                ],
            }
            print(f"[screen {screens[sig]['id']}] {focus}  ({len(screens[sig]['elements'])} clickable, shot={shot_bytes}B)")

        # skip elements whose tap opens a system app — never tap them, and
        # remember them so they aren't re-chosen
        SYSTEM_TRIGGERS = ("IMPORT VAULT", "IMPORT", "OPEN DOCUMENT", "BROWSE")

        # guard: launcher → relaunch; system picker/dialog spawned by our tap
        # → back out of it instead of exploring the system app
        if "com.android.launcher" in focus:
            print(f"  drifted to launcher — relaunching")
            launch_app(args.package)
            back_presses += 1
            continue
        if focus_component.startswith("com.android."):
            print("  in system UI — backing out")
            # mark the element that brought us here as tried, so we don't
            # tap it again (track last tap per screen)
            if last_tap and last_tap[0] in screens and last_tap[1]:
                pass  # already in tried set from the tap below
            adb("shell", "input", "keyevent", "4")
            back_presses += 1
            time.sleep(2.5)
            continue

        # pick first untried clickable element on this screen. Elements that
        # don't change the screen on tap (radios, show-password, prev/next on
        # a stuck slide) would block progress; if a whole screen is exhausted
        # we press back — but for wizard "Next" buttons, re-tap is harmless,
        # so allow re-trying navigation buttons once screen stuck is detected.
        def nav_rank(e):
            lbl = ((e["text"] or "") + (e["desc"] or "") + e["res"]).lower()
            if "next" in lbl or "forward" in lbl or "continue" in lbl:
                return 0
            if "previous" in lbl or "back" in lbl:
                return 2
            return 1

        chosen = None
        for e in sorted(screens[sig]["elements"], key=nav_rank):
            key = f"{e['x']},{e['y']}"
            if (sig, key) not in tried and e["y"] < 1230:  # skip navbar
                # skip pure system back/home buttons in app dumps
                if e["res"] in ("back", "home", "back_button"):
                    continue
                # skip elements that are known to leave the app (import/browse)
                label_txt = (e["text"] or e["desc"] or "").upper()
                if any(s in label_txt for s in SYSTEM_TRIGGERS):
                    tried.add((sig, key))
                    continue
                chosen = e
                break
        if not chosen and sig not in dead and stuck >= 2:
            # screen doesn't change no matter what → declare dead, back out
            dead.add(sig)
            print(f"  screen stuck ({sig[-12:]}) — declaring dead, back out")
            adb("shell", "input", "keyevent", "4")
            back_presses += 1
            stuck = 0
            time.sleep(2.5)
            continue

        if not chosen:
            # exhausted this screen → go back
            adb("shell", "input", "keyevent", "4")
            back_presses += 1
            time.sleep(2.5)
            continue

        key = f"{chosen['x']},{chosen['y']}"
        tried.add((sig, key))
        label = chosen["text"] or chosen["desc"] or chosen["res"] or chosen["class"]
        print(f"  tap ({chosen['x']},{chosen['y']}) {chosen['class']} '{label}'")
        tap(chosen["x"], chosen["y"])
        taps += 1
        back_presses = 0
        time.sleep(3)

        # track stuck-ness: did the screen change?
        if sig == last_sig:
            stuck += 1
        else:
            stuck = 0
        last_sig = sig

    result = {
        "package": args.package,
        "activity": args.activity,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "screen_count": len(screens),
        "taps_performed": taps,
        "screens": list(screens.values()),
    }
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nUI map: {out_path} — {len(screens)} screens, {taps} taps")


if __name__ == "__main__":
    main()
