#!/usr/bin/env python3
"""STB See phase for native desktop apps — macOS via Accessibility API (pyobjc).

Produces the same uimap JSON contract as stb/see_native.py (Linux AT-SPI):
{
  "package": "native:<app>",
  "platform": "macos-ax",
  "screen_count": N,
  "taps_performed": N,
  "generated": "...",
  "screens": [
    {"id": int, "focus": "window title", "sig": "...",
     "elements": [{"role": "...", "name": "...", "pos": [x, y]}], "screenshot": null}
  ]
}

NOT TESTED ON macOS YET — this VM is Linux. Written to the same contract
as the Linux traverser so bridge/appmap/scaffold/verify work unchanged.
Verify on a real Mac before shipping.

Usage:
    python3 stb/see_native_mac.py --app TextEdit [--max-actions 25 --out uimap.json]

Requires: pyobjc (pip install pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-Quartz)
          Accessibility permissions granted to Terminal/your IDE.
"""

import argparse
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time

try:
    import Quartz
    from Quartz import (
        AXUIElementCreateSystemWide, AXUIElementCopyAttributeValue,
        AXUIElementCopyActionNames, AXUIElementPerformAction,
        AXUIElementCopyElementChildren, kAXFocusedWindowAttribute,
        kAXTitleAttribute, kAXRoleAttribute, kAXValueAttribute,
        kAXPositionAttribute, kAXSizeAttribute, kAXChildrenAttribute,
        kAXPressAction, kAXRaiseAction,
    )
    _PYOBJC_OK = True
except Exception as exc:  # pyobjc not installed on this Linux VM
    _PYOBJC_OK = False
    _IMPORT_ERROR = exc

# Map macOS AX roles to STB actionable roles.
ROLE_MAP = {
    "AXButton": "push button",
    "AXCheckBox": "check box",
    "AXRadioButton": "radio button",
    "AXPopUpButton": "combo box",
    "AXTabGroup": "page tab",
    "AXMenuItem": "menu item",
    "AXMenu": "menu",
    "AXToolbarButton": "tool bar push button",
    "AXSlider": "slider",
    "AXScrollBar": "slider",
    "AXTable": "table cell",
    "AXTextField": "text",
    "AXTextArea": "text",
    "AXWindow": "window",
    "AXApplication": "application",
    "AXGroup": "panel",
    "AXImage": "image",
    "AXLink": "push button",
    "AXSeparator": "separator",
    "AXStaticText": "label",
}

ACTION_ROLES = {
    "push button", "toggle button", "check box", "radio button", "menu",
    "menu item", "combo box", "page tab", "tool bar push button",
    "tool bar toggle button", "table cell", "scale", "slider",
}

SKIP_ROLES = {"application", "window", "dialog", "filler", "panel",
              "root pane", "layered pane", "separator", "image",
              "static text", "label"}


def _ax(value):
    """Unwrap a CFRef to a Python value (str / int / list / dict)."""
    try:
        from Quartz import CFStringGetCStringPtr, CFArrayGetCount, \
            CFArrayGetValueAtIndex, CFDictionaryGetValue, CFGetTypeID, \
            CFStringGetTypeID, CFArrayGetTypeID, CFDictionaryGetTypeID, \
            CFNumberGetTypeID, CFNumberGetValue, kCFNumberSInt32Type
    except Exception:
        return value
    try:
        tid = CFGetTypeID(value)
    except Exception:
        return value
    if tid == CFStringGetTypeID():
        import ctypes
        buf = ctypes.create_string_buffer(1024)
        if CFStringGetCStringPtr(value, buf, 1024, 0):
            return ctypes.cast(buf, ctypes.c_char_p).value.decode(
                errors="replace")
        return str(value)
    if tid == CFArrayGetTypeID():
        return [_ax(CFArrayGetValueAtIndex(value, i))
                for i in range(CFArrayGetCount(value))]
    if tid == CFDictionaryGetTypeID():
        return value  # leave as CFRef; callers handle via _ax_dict
    if tid == CFNumberGetTypeID():
        v = ctypes.c_long(0)
        CFNumberGetValue(value, kCFNumberSInt32Type, ctypes.byref(v))
        return v.value
    return value


def _attr(elem, attr):
    try:
        rv, v = AXUIElementCopyAttributeValue(elem, attr, None)
        if rv == 0:  # kAXErrorSuccess
            return _ax(v)
    except Exception:
        pass
    return None


def _children(elem):
    try:
        rv, v = AXUIElementCopyElementChildren(elem, None)
        if rv == 0:
            return _ax(v) or []
    except Exception:
        pass
    return []


def _role_of(elem):
    return _attr(elem, kAXRoleAttribute) or "custom"


def _name_of(elem):
    return _attr(elem, kAXTitleAttribute) or _attr(elem, kAXValueAttribute) or ""


def _pos_size(elem):
    pos = _attr(elem, kAXPositionAttribute)
    size = _attr(elem, kAXSizeAttribute)
    if isinstance(pos, (list, tuple)) and len(pos) == 2:
        x, y = int(pos[0]), int(pos[1])
    else:
        x = y = 0
    if isinstance(size, (list, tuple)) and len(size) == 2:
        w, h = int(size[0]), int(size[1])
    else:
        w = h = 0
    return x, y, w, h


def sig_of(title, elements):
    blob = "|".join(f"{e['role']}:{e['name']}:{e['pos'][0]},{e['pos'][1]}"
                    for e in elements)
    return hashlib.sha1((title + "|" + blob).encode()).hexdigest()[:12]


def walk_children(elem, depth=0, max_depth=6):
    """Yield (elem, depth) for visible/enabled actionable children."""
    if depth > max_depth:
        return
    for c in _children(elem):
        role = _role_of(c)
        if role in SKIP_ROLES:
            yield from walk_children(c, depth + 1, max_depth)
            continue
        yield c, depth
        if role in ("AXApplication", "AXWindow", "AXGroup", "AXMenu",
                    "AXPopUpButton", "AXTabGroup", "AXGroup"):
            yield from walk_children(c, depth + 1, max_depth)


def capture_screen(app_elem, no_shot=False):
    """Return (sig, title, elements) for the focused window of app_elem."""
    title = _attr(app_elem, kAXTitleAttribute) or ""
    focused = _attr(app_elem, kAXFocusedWindowAttribute)
    win = focused if focused is not None else app_elem
    elements = []
    seen_keys = set()
    for c, _d in walk_children(win):
        role = ROLE_MAP.get(_role_of(c), "custom")
        if role not in ACTION_ROLES:
            continue
        name = _name_of(c)
        x, y, w, h = _pos_size(c)
        key = f"{role}:{name}" if name else f"{role}:{x},{y}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        if not (x or y or w or h) and not name:
            continue
        elements.append({"role": role, "name": name, "pos": [x + w // 2,
                                                             y + h // 2]})
    sig = sig_of(title, elements)
    shot = None
    if not no_shot:
        # macOS: use screencapture via subprocess (best-effort)
        try:
            shot = f"/tmp/stb_mac_shot_{len(elements)}.png"
            subprocess.run(["screencapture", "-x", shot], timeout=10,
                           capture_output=True)
            if not os.path.exists(shot):
                shot = None
        except Exception:
            shot = None
    return sig, title, elements, shot


def find_app(hint):
    """Find the running app matching hint (substring, case-insensitive)."""
    sys_wide = AXUIElementCreateSystemWide()
    apps = _attr(sys_wide, "AXWindowsAttribute") or []
    # AXWindowsAttribute on system-wide gives all windows; get owning app per window
    hint = (hint or "").lower()
    best = None
    for w in apps:
        try:
            title = _attr(w, kAXTitleAttribute) or ""
        except Exception:
            continue
        if hint and hint not in title.lower():
            continue
        if best is None:
            best = w
    return best


def click_at(x, y):
    try:
        import Quartz.CoreGraphics as CG
        ev_down = CG.CGEventCreateMouseEvent(
            None, CG.kCGEventLeftMouseDown,
            (float(x), float(y)), CG.kCGMouseButtonLeft)
        ev_up = CG.CGEventCreateMouseEvent(
            None, CG.kCGEventLeftMouseUp,
            (float(x), float(y)), CG.kCGMouseButtonLeft)
        CG.CGEventPost(CG.kCGHIDEventTap, ev_down)
        CG.CGEventPost(CG.kCGHIDEventTap, ev_up)
    except Exception:
        pass


def press_escape():
    try:
        import Quartz.CoreGraphics as CG
        ev_down = CG.CGEventCreateKeyboardEvent(None, 0x35, True)   # Escape
        ev_up = CG.CGEventCreateKeyboardEvent(None, 0x35, False)
        CG.CGEventPost(CG.kCGHIDEventTap, ev_down)
        CG.CGEventPost(CG.kCGHIDEventTap, ev_up)
    except Exception:
        pass


def perform_press(elem):
    try:
        actions = AXUIElementCopyActionNames(elem, None)
        if actions[0] == 0 and kAXPressAction in actions[1]:
            AXUIElementPerformAction(elem, kAXPressAction)
            return True
    except Exception:
        pass
    return False


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(
        description="STB See for macOS (AX API). Writes uimap JSON.")
    ap.add_argument("--app", required=True, help="app/window title substring hint")
    ap.add_argument("--max-actions", type=int, default=25)
    ap.add_argument("--out", required=True, help="uimap JSON output path")
    ap.add_argument("--no-shot", action="store_true",
                    help="skip screenshots")
    ap.add_argument("--traversal-log", default=None,
                    help="write tap/transition log here")
    args = ap.parse_args(argv)

    if not _PYOBJC_OK:
        print(f"ERROR: pyobjc not installed on this machine: {_IMPORT_ERROR}")
        print("Install with: pip install pyobjc-core pyobjc-framework-Cocoa "
              "pyobjc-framework-Quartz  (on macOS)")
        return 2

    win = find_app(args.app)
    if win is None:
        print(f"ERROR: no window matching '{args.app}' found via AX API")
        return 1

    try:
        AXUIElementPerformAction(win, kAXRaiseAction)
    except Exception:
        pass
    time.sleep(1.0)

    screens = []
    taps = 0
    tried = set()
    events = []

    def log(msg):
        events.append(msg)
        print(msg)

    cur = capture_screen(win, no_shot=args.no_shot)
    sig, title, elements, shot = cur
    screens.append({"id": 0, "focus": title, "sig": sig,
                    "elements": elements, "screenshot": shot})
    cur_sig = sig

    i = 0
    while taps < args.max_actions and i < len(elements):
        el = elements[i]
        i += 1
        key = f"{el['role']}:{el['name']}" if el["name"] else \
            f"{el['role']}:{el['pos'][0]},{el['pos'][1]}"
        if (cur_sig, key) in tried:
            continue
        tried.add((cur_sig, key))
        x, y = el["pos"]
        log(f"[tap {taps+1}/{args.max_actions}] {el['role']} "
            f"'{el['name'][:40]}' @ {x},{y}")
        # find the original AX element to perform press (best effort)
        clicked = False
        for c, _d in walk_children(win):
            cx, cy, cw, ch = _pos_size(c)
            if abs((cx + cw // 2) - x) < 5 and abs((cy + ch // 2) - y) < 5:
                clicked = perform_press(c) or clicked
                break
        if not clicked:
            click_at(x, y)
        taps += 1
        time.sleep(1.0)
        after = capture_screen(win, no_shot=args.no_shot)
        if after[0] != cur_sig:
            log(f"  -> new screen {after[0]} ({after[1][:40]!r})")
            screens.append({"id": len(screens), "focus": after[1],
                            "sig": after[0], "elements": after[2],
                            "screenshot": after[3]})
            cur_sig = after[0]
            elements = after[2]
            i = 0
            tried = set()
        # if dialog opened, press Escape honestly
        if after[1] and ("dialog" in after[1].lower()
                         or "alert" in after[1].lower()):
            log(f"[tap {taps+1}/{args.max_actions}] push button 'Cancel' @ (dialog)")
            taps += 1
            press_escape()
            time.sleep(1.0)
            after2 = capture_screen(win, no_shot=args.no_shot)
            if after2[0] != after[0]:
                log(f"  -> new screen {after2[0]} ({after2[1][:40]!r})")

    uimap = {
        "package": f"native:{args.app}",
        "platform": "macos-ax",
        "screen_count": len(screens),
        "taps_performed": taps,
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "screens": screens,
    }
    with open(args.out, "w") as f:
        json.dump(uimap, f, indent=2)
    log_path = args.traversal_log or (
        os.path.splitext(args.out)[0] + "_traversal.log")
    with open(log_path, "w") as f:
        f.write("\n".join(events) + "\n")
    print(f"traversal log: {log_path}")
    print(f"see-mac done: {len(screens)} screens, {taps} interactions -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
