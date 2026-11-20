#!/usr/bin/env python3
"""STB See phase for native desktop apps — Windows via UIA (pywinauto).

Produces the same uimap JSON contract as stb/see_native.py (Linux AT-SPI):
{
  "package": "native:<app>",
  "platform": "windows-uia",
  "screen_count": N,
  "taps_performed": N,
  "generated": "...",
  "screens": [
    {"id": int, "focus": "window title", "sig": "...",
     "elements": [{"role": "...", "name": "...", "pos": [x, y]}], "screenshot": null}
  ]
}

NOT TESTED ON WINDOWS YET — this VM is Linux. Written to the same contract
as the Linux traverser so bridge/appmap/scaffold/verify work unchanged.
Verify on a real Windows box before shipping.

Usage:
    python3 stb/see_native_win.py --app Notepad [--max-actions 25 --out uimap.json]

Requires: pywinauto (pip install pywinauto)
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

try:
    from pywinauto import Desktop
    from pywinauto.findwindows import ElementNotFoundError
    from pywinauto.controls.uiawrapper import UIAWrapper
except Exception as exc:  # pywinauto not installed on this Linux VM
    Desktop = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

# Map UIA control types / class names to STB actionable roles.
ROLE_MAP = {
    "Button": "push button",
    "CheckBox": "check box",
    "RadioButton": "radio button",
    "ComboBox": "combo box",
    "Tab": "page tab",
    "TabItem": "page tab",
    "MenuItem": "menu item",
    "Menu": "menu",
    "ToolBar": "tool bar push button",
    "Spinner": "scale",
    "Slider": "slider",
    "ListItem": "table cell",
    "DataItem": "table cell",
    "Text": "text",
    "Edit": "text",
    "Document": "text",
    "Pane": "panel",
    "Window": "window",
    "Dialog": "dialog",
    "Group": "panel",
    "ScrollBar": "slider",
    "Image": "image",
    "Hyperlink": "push button",
    "Separator": "separator",
    "TitleBar": "title bar",
    "MenuBar": "menu bar",
    "Custom": "custom",
}

ACTION_ROLES = {
    "push button", "toggle button", "check box", "radio button", "menu",
    "menu item", "combo box", "page tab", "tool bar push button",
    "tool bar toggle button", "table cell", "scale", "slider",
}

SKIP_ROLES = {"application", "desktop", "frame", "window", "dialog",
              "filler", "panel", "root pane", "layered pane", "separator",
              "menu bar", "title bar", "image", "custom"}


def _role_of(elem: UIAWrapper) -> str:
    try:
        ct = elem.element_info.control_type
    except Exception:
        return "custom"
    return ROLE_MAP.get(ct, "custom")


def _name_of(elem: UIAWrapper) -> str:
    # window_text() is the primary UIA name; element_info.name is the raw
    # UIA Name property; texts() covers legacy MSAA. Return the first
    # non-empty one.
    try:
        v = elem.window_text()
        if v and str(v).strip():
            return str(v).strip()
    except Exception:
        pass
    try:
        v = elem.element_info.name
        if v and str(v).strip():
            return str(v).strip()
    except Exception:
        pass
    try:
        v = elem.texts()
        if isinstance(v, list):
            v = " ".join(v)
        if v and str(v).strip():
            return str(v).strip()
    except Exception:
        pass
    return ""


def _center(elem: UIAWrapper):
    try:
        r = elem.rectangle()
        return ((r.left + r.right) // 2, (r.top + r.bottom) // 2)
    except Exception:
        return (0, 0)


def _visible(elem: UIAWrapper) -> bool:
    try:
        return bool(elem.is_visible())
    except Exception:
        return False


def _enabled(elem: UIAWrapper) -> bool:
    try:
        return bool(elem.is_enabled())
    except Exception:
        return True


def sig_of(title, elements):
    blob = "|".join(f"{e['role']}:{e['name']}:{e['pos'][0]},{e['pos'][1]}"
                    for e in elements)
    return hashlib.sha1((title + "|" + blob).encode()).hexdigest()[:12]


def walk_children(elem, depth=0, max_depth=6):
    """Yield (elem, depth) for visible, enabled, actionable children."""
    if depth > max_depth:
        return
    try:
        children = elem.children()
    except Exception:
        return
    for c in children:
        try:
            if not (_visible(c) and _enabled(c)):
                continue
        except Exception:
            continue
        role = _role_of(c)
        if role in SKIP_ROLES:
            # still descend into panels/windows to find actionable kids
            yield from walk_children(c, depth + 1, max_depth)
            continue
        yield c, depth
        # descend a little further for nested items (menu items, toolbar)
        if role in ("menu", "combo box", "page tab", "tool bar push button",
                    "panel", "custom", "window", "dialog"):
            yield from walk_children(c, depth + 1, max_depth)


def capture_screen(win, no_shot=False):
    """Return (sig, title, elements, shot, wrappers) for the focused window.

    `wrappers` maps element key -> UIAWrapper so the caller can click via the
    UIA Invoke pattern instead of raw screen coordinates.
    """
    title = ""
    try:
        title = win.window_text() or ""
    except Exception:
        pass
    elements = []
    wrappers = {}
    seen_keys = set()
    for c, _d in walk_children(win):
        role = _role_of(c)
        if role not in ACTION_ROLES:
            continue
        name = _name_of(c)
        x, y = _center(c)
        key = f"{role}:{name}" if name else f"{role}:{x},{y}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        if not (x or y) and not name:
            continue
        elements.append({"role": role, "name": name, "pos": [x, y]})
        wrappers[key] = c
    sig = sig_of(title, elements)
    shot = None
    if not no_shot:
        try:
            img = win.capture_as_image()
            if img is not None:
                shot = f"win_shot_{len(elements)}.png"
                img.save(shot)
        except Exception:
            pass
    return sig, title, elements, shot, wrappers


def find_app(hint):
    """Find the top-level window matching hint (substring, case-insensitive)."""
    if Desktop is None:
        raise RuntimeError(f"pywinauto not available: {_IMPORT_ERROR}")
    hint = (hint or "").lower()
    matches = []
    for w in Desktop(backend="uia").windows():
        try:
            title = w.window_text() or ""
        except Exception:
            continue
        if hint and hint in title.lower():
            matches.append(w)
        elif not hint:
            matches.append(w)
    # Prefer a window that actually has children (not an empty registration node)
    alive = []
    for w in matches:
        try:
            if len(w.children()) > 0:
                alive.append(w)
        except Exception:
            continue
    return (alive or matches)[0] if (alive or matches) else None


def click_at(x, y):
    import ctypes
    ctypes.windll.user32.SetCursorPos(x, y)
    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)  # left down
    ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)  # left up


def click_elem(elem: UIAWrapper, x, y):
    """Click an element. Prefer the UIA Invoke pattern (works even when the
    window is not in the foreground / on a headless CI runner); fall back to
    a real mouse event at screen coordinates when Invoke is unavailable.
    """
    try:
        if elem is not None:
            inv = elem.invoke()
            if inv is not False:
                return
    except Exception:
        pass
    try:
        if elem is not None:
            elem.click_input()
            return
    except Exception:
        pass
    click_at(x, y)


def send_keys(keys):
    import ctypes
    # minimal: only Escape and Alt+N supported here; extend as needed
    VK_ESCAPE = 0x1B
    VK_N = 0x4E
    VK_MENU = 0x12  # Alt
    KEYEVENTF_KEYUP = 0x0002
    if keys.lower() == "{esc}":
        ctypes.windll.user32.keybd_event(VK_ESCAPE, 0, 0, 0)
        ctypes.windll.user32.keybd_event(VK_ESCAPE, 0, KEYEVENTF_KEYUP, 0)
    elif keys.lower() == "%n":  # pywinauto % = Alt
        ctypes.windll.user32.keybd_event(VK_MENU, 0, 0, 0)
        ctypes.windll.user32.keybd_event(VK_N, 0, 0, 0)
        ctypes.windll.user32.keybd_event(VK_N, 0, KEYEVENTF_KEYUP, 0)
        ctypes.windll.user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(
        description="STB See for Windows (UIA). Writes uimap JSON.")
    ap.add_argument("--app", required=True, help="window title substring hint")
    ap.add_argument("--max-actions", type=int, default=25)
    ap.add_argument("--out", required=True, help="uimap JSON output path")
    ap.add_argument("--no-shot", action="store_true",
                    help="skip screenshots")
    ap.add_argument("--traversal-log", default=None,
                    help="write tap/transition log here")
    args = ap.parse_args(argv)

    if Desktop is None:
        print(f"ERROR: pywinauto not installed on this machine: {_IMPORT_ERROR}")
        print("Install with: pip install pywinauto  (on Windows)")
        return 2

    win = find_app(args.app)
    if win is None:
        print(f"ERROR: no window matching '{args.app}' found via UIA")
        return 1

    try:
        win.set_focus()
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
    if not cur[2]:
        log(f"[see-win] no actionable elements in '{cur[1]}'")

    # Simple BFS: click untried actionable element, register new screen on change.
    sig, title, elements, shot, wrappers = cur
    screens.append({"id": 0, "focus": title, "sig": sig,
                    "elements": elements, "screenshot": shot})
    cur_sig, cur_img_elems = sig, elements

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
        click_elem(wrappers.get(key), x, y)
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
            wrappers = after[4]
            i = 0
            tried = set()
        # if title changed to a dialog, try Escape to close it honestly
        if after[1] and ("dialog" in after[1].lower()
                         or "question" in after[1].lower()):
            log(f"[tap {taps+1}/{args.max_actions}] push button 'Cancel' @ (dialog)")
            taps += 1
            send_keys("{esc}")
            time.sleep(1.0)
            after2 = capture_screen(win, no_shot=args.no_shot)
            if after2[0] != after[0]:
                log(f"  -> new screen {after2[0]} ({after2[1][:40]!r})")

    uimap = {
        "package": f"native:{args.app}",
        "platform": "windows-uia",
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
    print(f"see-win done: {len(screens)} screens, {taps} interactions -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
