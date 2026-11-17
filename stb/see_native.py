#!/usr/bin/env python3
"""STB See phase for native desktop apps (Linux X11 via AT-SPI).

Runtime UI traversal for desktop applications — the Linux counterpart of
stb_traverse.py (Android/adb/uiautomator). Uses the AT-SPI accessibility
tree (the same channel screen readers use) to enumerate windows, menus,
buttons, text fields, and their positions; drives interaction via xdotool.

Requirements (Ubuntu 24.04):
  sudo apt install python3-pyatspi at-spi2-core dbus-x11 xdotool
  A running X session (Xvfb + xfce4 works; DISPLAY must be set).
  GUI apps expose their a11y tree over the session bus at
  DBUS_SESSION_BUS_ADDRESS (default unix:path=/run/user/<uid>/bus).

Usage:
  python3 stb_see_native.py --app mousepad [--file /tmp/x.txt]
      [--max-actions 25 --out uimap_native.json]

Semantics mirror the Android traverser:
  - screen signature = focused frame title + hash of element list
  - BFS over actionable elements (buttons, menu items, check boxes)
  - stuck detection, per-screen element evidence, screenshots via import(1)
  - output JSON UI map compatible with stb_bridge.py layout (screens[].elements)
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

os.environ.setdefault("DISPLAY", ":1")
UID = os.getuid()
os.environ.setdefault(
    "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{UID}/bus")

import pyatspi  # noqa: E402  (needs DISPLAY env set first)

# Roles treated as interactable during traversal.
ACTION_ROLES = {
    "push button", "toggle button", "check box", "radio button", "menu",
    "menu item", "combo box", "page tab", "tool bar push button",
    "tool bar toggle button",
}
SKIP_ROLES = {"application", "desktop frame", "frame", "window", "dialog",
              "filler", "panel", "root pane", "layered pane", "separator"}


def sh(cmd, timeout=15):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout
    except Exception as exc:
        return f"ERR {exc}"


class ATSPIWalker:
    """Enumerate the AT-SPI tree of one application."""

    def __init__(self, app_name_hint):
        self.hint = (app_name_hint or "").lower()

    def find_app(self):
        desktop = pyatspi.Registry.getDesktop(0)
        apps = list(desktop)
        if self.hint:
            for a in apps:
                if self.hint in (a.name or "").lower():
                    return a
        return apps[0] if apps else None

    def elements(self, app, max_depth=8):
        """Yield dicts: role, name, coords, actions, text, states."""
        out = []

        def walk(node, depth):
            if depth > max_depth:
                return
            try:
                role = node.getRoleName()
            except Exception:
                return
            entry = None
            pos = None
            try:
                comp = node.queryComponent()
                e = comp.getExtents(pyatspi.DESKTOP_COORDS)
                vals = (e.x, e.y, e.width, e.height)
                if all(v is not None and -1 <= v <= 100000 for v in vals):
                    pos = vals
            except Exception:
                pos = None
            acts = []
            try:
                act = node.queryAction()
                acts = [act.getName(i) for i in range(act.getNActions())]
            except Exception:
                pass
            txt = None
            try:
                txt = node.queryText().getText(0, -1)
            except Exception:
                pass
            st = node.getState()
            states = [nm.replace("STATE_", "").lower()
                      for nm in ("ACTIVE", "FOCUSABLE", "EDITABLE", "SHOWING",
                                 "CHECKED", "SELECTED", "ENABLED")
                      if getattr(pyatspi, "STATE_" + nm, None) is not None
                      and st.contains(getattr(pyatspi, "STATE_" + nm))]
            if role not in SKIP_ROLES or acts:
                entry = {
                    "role": role,
                    "name": (node.name or "").strip(),
                    "pos": pos,
                    "actions": acts,
                    "text": txt,
                    "states": states,
                }
                out.append(entry)
            for c in node:
                walk(c, depth + 1)

        walk(app, 0)
        return out


class NativeTraverser:
    def __init__(self, app_hint, max_actions=25, shot=True):
        self.hint = app_hint
        self.max_actions = max_actions
        self.shot = shot
        self.walker = ATSPIWalker(app_hint)
        self.screens = []
        self.seen_sigs = set()
        self.tried = set()  # (screen_sig, element_key) pairs already clicked
        self.total_taps = 0
        self.spawned_from = 0  # clicks that created a new window/tab

    @staticmethod
    def _spawns_blank_window(el):
        """Heuristic: menu items that typically create empty windows/tabs."""
        name = (el.get("name") or "").lower()
        return name in ("new", "new tab", "new window", "new file",
                        "new document")

    # --- screen capture -------------------------------------------------
    def screenshot(self, path):
        if not self.shot:
            return None
        # prefer scrot (small, X11-native); fall back to ImageMagick import
        for cmd in (["scrot", "-o", path], ["import", "-window", "root", path]):
            r = sh(cmd, timeout=20)
            if not r.startswith("ERR") and os.path.exists(path) \
                    and os.path.getsize(path) > 0:
                return path
        return None

    # --- interaction ----------------------------------------------------
    def activate_window(self, title_hint):
        out = sh(["xdotool", "search", "--name", title_hint])
        for wid in out.split():
            sh(["xdotool", "windowactivate", "--sync", wid])
            return wid
        return None

    def click_center(self, pos):
        if not pos:
            return False
        x, y, w, h = pos
        cx, cy = x + w // 2, y + h // 2
        sh(["xdotool", "mousemove", "--sync", str(cx), str(cy)])
        sh(["xdotool", "click", "1"])
        return True

    # --- traversal ------------------------------------------------------
    def _frame_title(self, app):
        try:
            for c in app:
                if c.getRoleName() == "frame":
                    return (c.name or "").strip()
        except Exception:
            pass
        return self.hint

    # Roles whose text/state changes just from hovering a menu or moving the
    # caret — they must NOT define a new screen.
    TRANSIENT_ROLES = {"status bar", "menu", "menu item", "separator",
                       "tooltip", "scroll bar", "icon", "alert", "label"}

    def _sig(self, title, elements):
        # Structural screen identity: frame title + stable, visible widgets.
        # Menu opens/hover text are noise, not new screens (unlike Android's
        # activity changes). Filter them out so toggling File/Edit does not
        # inflate screen_count.
        stable = [
            f"{e['role']}:{e['name']}" for e in elements
            if e["role"] not in self.TRANSIENT_ROLES
            and "showing" in e.get("states", [])
        ]
        blob = title + "|" + json.dumps(sorted(stable), sort_keys=True)
        return hashlib.sha1(blob.encode()).hexdigest()[:12], title

    def capture_screen(self, register=True):
        app = self.walker.find_app()
        if app is None:
            return None
        title = self._frame_title(app)
        elements = self.walker.elements(app)
        sig, title = self._sig(title, elements)
        # active frame name may start with '*'; normalize
        if register and sig not in self.seen_sigs:
            self.seen_sigs.add(sig)
            shot = self.screenshot(
                os.path.join(self.out_dir, f"nat_{len(self.screens)}.png"))
            self.screens.append({
                "id": len(self.screens),
                "focus": title,
                "sig": sig,
                "elements": elements,
                "screenshot": shot,
            })
        return sig, title, elements

    def interactables(self, elements):
        out = []
        for e in elements:
            if e["role"] in ACTION_ROLES and e.get("pos") and e["name"]:
                key = f"{e['role']}:{e['name']}"
                out.append((key, e))
        return out

    def run(self, out_dir):
        self.out_dir = out_dir
        os.makedirs(out_dir, exist_ok=True)
        taps = 0
        relaunches = 0
        while taps < self.max_actions:
            cur = self.capture_screen()
            if cur is None:
                print("[see-native] app not visible — done")
                break
            sig, _title, elements = cur
            todo = [(k, e) for k, e in self.interactables(elements)
                    if (sig, k) not in self.tried]
            # Anti-thrash: opening a top-level menu must not eat the budget.
            # Menus are cheap and identical across tabs — dedupe them GLOBALLY
            # (by element key, not per-sig) so 'File' is tapped once per app,
            # and skip 'New'-style items that only spawn empty windows/tabs
            # when we have already done so (thrash guard).
            todo = [(k, e) for k, e in todo
                    if e["role"] != "menu" or
                    not any(k2 == k for (_s, k2) in self.tried)]
            spawned = self.spawned_from > 3
            if spawned:
                todo = [(k, e) for k, e in todo
                        if not self._spawns_blank_window(e)]
            if not todo:
                # per-screen exhausted — but prefer global menu exploration:
                # menus (File/Edit/View/Help) should each be opened once even
                # if the window title changed (new tab). Prefer an untried
                # top-level menu before giving up.
                menus = [(k, e) for k, e in self.interactables(elements)
                         if e["role"] == "menu"
                         and all(k2 != k for (_s, k2) in self.tried)]
                if menus and relaunches < 6:
                    relaunches += 1
                    key, el = menus[0]
                    self.tried.add((sig, key))
                    print(f"[explore] open menu '{el['name']}'")
                    self.click_center(el["pos"])
                    taps += 1
                    self.total_taps = taps
                    time.sleep(1.2)
                    continue
                print(f"[see-native] screen {sig} exhausted")
                if relaunches < 1:
                    relaunches += 1
                    sh(["xdotool", "key", "--clearmodifiers", "Escape"])
                    time.sleep(1)
                    continue
                break
            key, el = todo[0]
            self.tried.add((sig, key))
            print(f"[tap {taps+1}/{self.max_actions}] {el['role']} "
                  f"'{el['name'][:40]}' @ {el['pos'][:2]}")
            before = self.capture_screen(register=False)
            self.click_center(el["pos"])
            taps += 1
            self.total_taps = taps
            time.sleep(1.2)
            after = self.capture_screen()
            if after and after[0] != before[0]:
                after_title = after[1]
                # did this click spawn an empty window/tab?
                if "Untitled" in after_title or \
                        ("new" in el["name"].lower() and
                         "untitled" in after_title.lower()):
                    self.spawned_from += 1
                print(f"  -> new screen {after[0]} ({after_title[:40]!r})")
            # close menus/dialogs that opened to keep BFS flat
            if after and any("menu" in e["role"] for e in after[2]):
                pass  # menu contents are already part of the tree
        print(f"[see-native] done: {len(self.screens)} screens, "
              f"{taps} interactions")
        return self.screens


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", required=True, help="application name hint "
                    "(matched against AT-SPI application name)")
    ap.add_argument("--max-actions", type=int, default=25)
    ap.add_argument("--out", required=True, help="uimap JSON output path")
    ap.add_argument("--no-shot", action="store_true",
                    help="skip screenshots (import may be absent)")
    args = ap.parse_args(argv)

    out_dir = os.path.splitext(args.out)[0] + "_shots"
    t = NativeTraverser(args.app, args.max_actions, not args.no_shot)
    t.run(out_dir)
    taps = t.total_taps

    uimap = {
        "package": f"native:{args.app}",
        "platform": "linux-x11-atspi",
        "screen_count": len(t.screens),
        "taps_performed": taps,
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "screens": t.screens,
    }
    with open(args.out, "w") as f:
        json.dump(uimap, f, indent=1)
    print(f"uimap: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
