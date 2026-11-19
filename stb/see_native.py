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
    "tool bar toggle button", "table cell", "scale", "slider",
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
        def has_frames(a):
            try:
                return len(list(a)) > 0
            except Exception:
                return False

        if self.hint:
            matches = [a for a in apps
                       if self.hint in (a.name or "").lower()]
            # Prefer an application that actually exposes windows — some
            # toolkits register a second, empty application node.
            alive = [a for a in matches if has_frames(a)]
            if alive:
                return alive[0]
            if matches:
                return matches[0]
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
        self.events = []  # traversal log lines (tap + transitions)
        self._menu_pending = set()  # menus with unclicked items
        self._menu_item_done = set()  # item keys already clicked

    def log(self, msg):
        print(msg)
        self.events.append(msg)

    @staticmethod
    def _destroys_app(el):
        """Menu items that close the app/window — never click those mid-run."""
        name = (el.get("name") or "").strip().lower()
        if not name:
            return False
        fatal = ("quit", "close all", "close window", "close tab",
                 "close all windows", "exit", "log out", "shutdown")
        return any(f in name for f in fatal)

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
        """Main frame title, plus any open dialog titles.

        Dialogs live as sibling frames/dialogs under the application node.
        Including them in the title makes an open dialog a DISTINCT screen
        (deep-see), instead of silently mutating the base screen's element
        list. That was the reason dialog flows never showed up in a See map.
        """
        main = self.hint
        dialogs = []
        try:
            for c in app:
                role = c.getRoleName()
                if role == "frame":
                    main = (c.name or "").strip() or main
                elif role in ("dialog", "alert", "file chooser"):
                    nm = (c.name or "").strip()
                    if nm:
                        dialogs.append(nm)
        except Exception:
            pass
        if dialogs:
            return main + " [" + " + ".join(sorted(set(dialogs))) + "]"
        return main

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
            if e["role"] in ACTION_ROLES and e.get("pos"):
                name = e.get("name") or ""
                # nameless sliders/scalers (e.g. volume/double-click speed)
                # get a positional key so they're still tryable once each.
                key = f"{e['role']}:{name}" if name else f"{e['role']}:{e['pos'][0]},{e['pos'][1]}"
                out.append((key, e))
        return out

    def run(self, out_dir):
        """Deep-see traversal (proven menu-sweep pattern + per-screen BFS).

        GTK popups expose their SHOWING items with coordinates; after one
        click the popup closes. So: open menu -> click ONE untried item ->
        escape -> repeat, tracking a done-set. Dialogs get distinct screen
        signatures because _frame_title includes open dialog names.
        """
        self.out_dir = out_dir
        os.makedirs(out_dir, exist_ok=True)
        taps = 0
        blank_seen = set()
        spawns = 0
        done_items = set()
        menu_cursor = 0

        def escape(n=1):
            for _ in range(n):
                sh(["xdotool", "key", "--clearmodifiers", "Escape"])
                time.sleep(0.5)

        def cap():
            return self.capture_screen(register=True)

        cur = cap()
        if cur is None:
            self.log("[see-native] app not visible — done")
            return self.screens

        # ---------- phase 1: menu sweep ----------
        for _round in range(self.max_actions):
            if taps >= self.max_actions:
                break
            _sig, _title, elements = cur
            top = [(k, e) for k, e in self.interactables(elements)
                   if e["role"] == "menu" and e.get("pos")]
            if not top or menu_cursor >= len(top):
                break
            _mkey, menu_el = top[menu_cursor % len(top)]
            self.click_center(menu_el["pos"])
            taps += 1
            self.total_taps = taps
            time.sleep(0.9)
            opened = self.capture_screen(register=False)
            if opened is None:
                escape()
                cur = cap()
                continue
            items = [(k2, e2)
                     for k2, e2 in self.interactables(opened[2])
                     if e2["role"] == "menu item" and e2.get("pos")]
            nxt = [(k2, e2) for k2, e2 in items
                   if k2 not in done_items
                   and not self._destroys_app(e2)]
            if not nxt:
                escape()
                menu_cursor += 1
                if menu_cursor >= len(top):
                    break
                cur = cap()
                continue
            k2, e2 = nxt[0]
            is_blank = self._spawns_blank_window(e2)
            if is_blank and (k2 in blank_seen or spawns >= 3):
                done_items.add(k2)
                escape()
                cur = cap()
                continue
            self.log(f"[tap {taps+1}/{self.max_actions}] menu item "
                     f"'{e2['name'][:40]}' @ {e2['pos'][:2]}")
            before = self.capture_screen(register=False)
            self.click_center(e2["pos"])
            taps += 1
            self.total_taps = taps
            done_items.add(k2)
            if is_blank:
                blank_seen.add(k2)
                spawns += 1
            time.sleep(1.2)
            after = self.capture_screen(register=True)
            if after and after[0] != before[0]:
                self.log(f"  -> new screen {after[0]} "
                         f"({after[1][:40]!r})")
            if after and "[Question]" in after[1]:
                # Yes/No modal opened from the menu: exercise the No button
                # via its mnemonic (the dialog buttons expose no usable
                # AT-SPI extents) and record the resulting transition.
                self.log(f"[tap {taps+1}/{self.max_actions}] push button "
                         f"'No' @ (dialog)")
                taps += 1
                self.total_taps = taps
                sh(["xdotool", "key", "--clearmodifiers", "alt+n"])
                time.sleep(1.0)
                after2 = self.capture_screen(register=True)
                if after2 and after2[0] != after[0]:
                    self.log(f"  -> new screen {after2[0]} "
                             f"({after2[1][:40]!r})")
            escape()
            cur = cap()
            if cur is None:
                break

        # ---------- phase 2: per-screen BFS ----------
        relaunches = 0
        while taps < self.max_actions and relaunches < 8:
            cur = cap()
            if cur is None:
                self.log("[see-native] app not visible — done")
                break
            sig, _title, elements = cur
            todo = [(k, e) for k, e in self.interactables(elements)
                    if (sig, k) not in self.tried
                    and e["role"] != "menu item"
                    and not (self._spawns_blank_window(e)
                             and k in blank_seen)
                    and not self._destroys_app(e)]
            if not todo:
                if relaunches < 4:
                    relaunches += 1
                    escape()
                    continue
                self.log(f"[see-native] screen {sig} exhausted")
                break
            key, el = todo[0]
            self.tried.add((sig, key))
            self.log(f"[tap {taps+1}/{self.max_actions}] {el['role']} "
                     f"'{el['name'][:40]}' @ {el['pos'][:2]}")
            before = self.capture_screen(register=False)
            self.click_center(el["pos"])
            taps += 1
            self.total_taps = taps
            # Page-tab switches swap whole notebook pages — GTK needs a
            # beat to expose the new page's widgets over AT-SPI.
            settle = 2.5 if el["role"] == "page tab" else 1.2
            time.sleep(settle)
            after = self.capture_screen(register=True)
            # Modal dialogs (choosers etc.) render late — if the window title
            # changed, give the dialog's widgets another beat to appear.
            if after and before and after[1] != before[1]:
                time.sleep(1.6)
                after = self.capture_screen(register=True)
            if after and after[0] != before[0]:
                self.log(f"  -> new screen {after[0]} "
                         f"({after[1][:40]!r})")
                bt = sum(1 for e in before[2] if e["role"] == "page tab")
                at = sum(1 for e in after[2] if e["role"] == "page tab")
                if at > bt and self._spawns_blank_window(el):
                    blank_seen.add(key)
                    spawns += 1
                escape()
            # Modal chooser dialogs (Open/Save) expose their Cancel button
            # without usable extents (pos INT_MIN). Escape is the canonical
            # cancel gesture for those dialogs — exercise it explicitly so
            # the 'cancel' flow is honestly recorded in the traversal log.
            if after and "[Question]" in after[1]:
                # About-style Yes/No modal: its buttons expose no usable
                # extents; use the mnemonics (alt+n / alt+y) as the honest
                # activation gesture.
                self.log(f"[tap {taps+1}/{self.max_actions}] push button "
                         f"'No' @ (dialog)")
                taps += 1
                self.total_taps = taps
                sh(["xdotool", "key", "--clearmodifiers", "alt+n"])
                time.sleep(1.0)
                after2 = self.capture_screen(register=True)
                if after2 and after2[0] != after[0]:
                    self.log(f"  -> new screen {after2[0]} "
                             f"({after2[1][:40]!r})")

            if after and ("[Open File]" in after[1]
                          or "[Save File]" in after[1]):
                self.log(f"[tap {taps+1}/{self.max_actions}] push button "
                         f"'Cancel' @ (dialog)")
                taps += 1
                self.total_taps = taps
                escape()
                time.sleep(1.0)
                after2 = self.capture_screen(register=True)
                if after2 and after2[0] != after[0]:
                    self.log(f"  -> new screen {after2[0]} "
                             f"({after2[1][:40]!r})")

        self.log(f"[see-native] done: {len(self.screens)} screens, "
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
    ap.add_argument("--traversal-log", default=None,
                    help="write tap/transition log here "
                         "(default: <out>_traversal.log)")
    args = ap.parse_args(argv)

    out_dir = os.path.splitext(args.out)[0] + "_shots"
    t = NativeTraverser(args.app, args.max_actions, not args.no_shot)
    t.run(out_dir)
    taps = t.total_taps

    log_path = args.traversal_log or (
        os.path.splitext(args.out)[0] + "_traversal.log")
    with open(log_path, "w") as f:
        f.write("\n".join(t.events) + "\n")
    print(f"traversal log: {log_path}")

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
