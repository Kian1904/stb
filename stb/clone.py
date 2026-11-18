#!/usr/bin/env python3
"""STB Clone — decompose an app into build parts, easy -> hard.

Reads the AppMap (stb/appmap.py output) and produces a clone plan:
each part is a coherent slice of functionality with

  - toolchain needed to build/test it (per platform)
  - dependency on earlier parts
  - difficulty score (0-100) and the reasons behind it
  - the concrete acceptance evidence: which UI elements/functions the
    finished part must show, and which appmap functions map to it

Parts are sorted easy -> hard so an agent (or human) builds the simplest
runnable slice first, verifies it with STB's own See phase (screen diff),
then climbs. The final plan is written back into appmap["build_parts"]
and can be exported standalone (plan JSON) for a build agent.

Scoring inputs (all derived from data, no guessing):
  element count of the part's screens
  dynamic/interactive roles (entry, canvas, webview = harder)
  external-dependency evidence (ext-dep needs extra packages)
  runtime-generated handlers (need reimplementing logic, not just layout)
  flows crossing into other parts

Usage:
  python3 stb/clone.py <appmap.json> [--target android|native-gtk]
      [--out plan.json] [--write-back]
"""
import argparse
import json
import sys


# --------------------------------------------------------------------------
# role difficulty weights (per single element)
# --------------------------------------------------------------------------
ROLE_COST = {
    # trivial static/structural
    "label": 1, "text view": 1, "image": 1, "image view": 1, "menu bar": 2,
    "menu": 3, "menu item": 3, "page tab": 3, "separator": 0,
    # interactive widgets
    "push button": 4, "check box": 4, "radio button": 4, "toggle button": 5,
    "combo box": 6, "spin button": 6, "slider": 6, "scroll bar": 3,
    "entry": 6, "text": 10,       # editable text = stateful logic
    "progress bar": 5, "status bar": 4, "tool bar": 6,
    # heavy surfaces
    "canvas": 25, "drawing area": 25, "web view": 30, "webview": 30,
    "list": 8, "list item": 5, "table": 10, "tree": 10, "panel": 4,
    "dialog": 8, "alert dialog": 6, "popup window": 8,
    "floating action button": 5, "action bar": 6, "toolbar": 6,
    "drawer": 8, "bottom navigation": 8, "recycler view": 10,
    "card view": 4, "linear layout": 2, "frame layout": 2,
    "constraint layout": 3, "relative layout": 2, "scroll view": 3,
}
DEFAULT_ROLE_COST = 5

# roles that nearly always mean "reimplement real logic"
HARD_ROLES = {"canvas", "drawing area", "web view", "webview", "text",
              "entry", "recycler view", "table", "tree"}

EVIDENCE_VIA_COST = {
    "source-string": 0,      # layout-level, cheap
    "ext-dep": 4,            # needs the dependency in the clone
    "runtime-generated": 12, # logic lives in runtime, must be re-coded
    "unresolved": 6,         # unknown origin — needs investigation
}


def clamp(x, lo=0, hi=100):
    return max(lo, min(hi, x))


def role_cost(role):
    role = (role or "").lower()
    for k, v in ROLE_COST.items():
        if k in role:
            return v
    return DEFAULT_ROLE_COST


# --------------------------------------------------------------------------
# part extraction: slice the appmap into coherent functional parts
# --------------------------------------------------------------------------

def part_of_function(fn):
    """Assign each function to a coarse part bucket by its evidence and
    role. Buckets are the classic app slices, easy first."""
    ev = fn.get("evidence") or []
    vias = {e.get("via") for e in ev}
    role = (fn.get("role") or "").lower()

    if role in HARD_ROLES:
        return "content-engine"
    if "runtime-generated" in vias:
        return "runtime-behavior"
    if "ext-dep" in vias:
        return "external-integration"
    if "dialog" in role or "alert" in role or "popup" in role:
        return "dialogs"
    if role in ("menu", "menu item", "menu bar", "toolbar", "tool bar",
                "action bar"):
        return "menus-actions"
    if "check box" in role or "checkbox" in role or "radio" in role \
            or "combo" in role or "spinner" in role or "switch" in role \
            or "toggle" in role:
        return "settings-controls"
    if "button" in role or "tab" in role or "fab" in role:
        return "basic-widgets"
    if "menu" in role or "tool bar" in role or "toolbar" in role \
            or "action bar" in role or "options" in role:
        return "menus-actions"
    if "edit" in role or "text field" in role:
        return "content-engine"
    return "static-ui"


PART_META = {
    # key: (title, description, difficulty baseline)
    "static-ui": ("Static UI skeleton",
                  "Windows, screens, static labels, images, layout only",
                  5),
    "basic-widgets": ("Basic widgets",
                      "Buttons and tabs with click handlers", 15),
    "menus-actions": ("Menus & actions",
                      "Menu bars, toolbars, action wiring", 25),
    "settings-controls": ("Settings & toggles",
                          "Checkboxes, radios, combos — persisted prefs",
                          35),
    "dialogs": ("Dialogs & popups",
                "Modal flows: open/save, confirmations, about", 45),
    "external-integration": ("External dependencies",
                             "Features backed by ext libs (theme engines, "
                             "parsers, codecs)", 60),
    "runtime-behavior": ("Runtime behaviors",
                         "Logic that only exists at runtime: session "
                         "restore, dynamic state, generated strings", 75),
    "content-engine": ("Content engine",
                       "The hard core: text editor, canvas, webview, "
                       "game logic — the reason the app exists", 90),
}
PART_ORDER = list(PART_META)


def group_functions(functions):
    buckets = {k: [] for k in PART_ORDER}
    for fn in functions:
        buckets[part_of_function(fn)].append(fn)
    return buckets


# --------------------------------------------------------------------------
# toolchain per target
# --------------------------------------------------------------------------

TOOLCHAIN = {
    "android": {
        "common": ["openjdk-17", "android-sdk (aapt2, d8, apksigner)",
                   "gradle"],
        "per-part": {
            "static-ui": ["constraint-layout"],
            "content-engine": ["(depends: webview is platform; canvas is "
                               "android.graphics; text editor is custom)"],
        },
    },
    "native-gtk": {
        "common": ["meson", "ninja", "libgtk-3-dev", "pkg-config"],
        "per-part": {
            "menus-actions": ["libgtk-3-dev (GtkBuilder .ui)"],
            "settings-controls": ["glib-compile-resources", "GSettings"],
            "dialogs": ["libgtk-3-dev (GtkFileChooser, GtkMessageDialog)"],
            "external-integration": ["gtksourceview-4 (libgtksourceview-4-dev)"],
            "content-engine": ["libgtksourceview-4-dev (GtkSourceBuffer)"],
        },
    },
}


def screen_ids_for_functions(functions, screens_by_id):
    ids = {fn["screen"] for fn in functions}
    return sorted(i for i in ids if i in screens_by_id)


def build_parts(appmap, target):
    screens_by_id = {s["id"]: s for s in appmap["screens"]}
    buckets = group_functions(appmap["functions"])

    parts = []
    rank = 0
    for key in PART_ORDER:
        fns = buckets[key]
        if not fns:
            continue
        rank += 1
        meta = PART_META[key]
        # difficulty: baseline + component pressure from this bucket's roles
        comp = sum(role_cost(f.get("role")) for f in fns)
        hard_elems = sum(1 for f in fns
                         if (f.get("role") or "").lower() in HARD_ROLES)
        diff = clamp(meta[2] + min(comp // 10, 30))

        screen_ids = screen_ids_for_functions(fns, screens_by_id)
        # element count those screens contribute to this part
        elems = [fn["element"] for fn in fns]

        tools = list(TOOLCHAIN[target]["common"])
        tools += TOOLCHAIN[target]["per-part"].get(key, [])

        parts.append({
            "id": rank,
            "key": key,
            "title": meta[0],
            "description": meta[1],
            "difficulty": diff,
            "difficulty_reasons": {
                "baseline": meta[2],
                "element_cost_sum": comp,
                "hard_elements": hard_elems,
            },
            "screens": screen_ids,
            "functions": [fn["element"] for fn in fns][:40],
            "function_count": len(fns),
            "evidence_kinds": sorted({e["via"] for fn in fns
                                      for e in (fn.get("evidence") or [])}),
            "tools_needed": tools,
            "depends_on": [],   # filled after sort: any earlier part whose
                                # screens/roles this one's screens reuse
            "acceptance": {
                # STB round-trip check: these elements must appear in the
                # clone's See phase with matching labels
                "must_see_elements": elems[:20],
            },
        })

    # sort easy -> hard by actual difficulty, renumber, chain deps on the
    # sorted order
    parts.sort(key=lambda p: (p["difficulty"], p["id"]))
    key_by_old = {p["id"]: p["key"] for p in parts}
    for i, p in enumerate(parts, 1):
        p["id"] = i
    for i, p in enumerate(parts):
        p["depends_on"] = ([parts[i - 1]["id"]] if i > 0 else [])
    if any(p["key"] == "content-engine" for p in parts):
        first_hard = min((p["id"] for p in parts
                          if p["key"] == "content-engine"), default=None)
        if first_hard and first_hard > 1:
            parts[[i for i, p in enumerate(parts)
                   if p["id"] == first_hard][0]]["depends_on"] = [i2 + 1
                    for i2 in range(first_hard - 1)]
    return parts


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser()
    ap.add_argument("appmap")
    ap.add_argument("--target", default=None,
                    choices=["android", "native-gtk"],
                    help="clone target (default: from appmap platform)")
    ap.add_argument("--out", default=None, help="standalone plan JSON")
    ap.add_argument("--write-back", action="store_true",
                    help="also write build_parts into the appmap file")
    args = ap.parse_args(argv)

    appmap = json.load(open(args.appmap))
    platform = appmap.get("meta", {}).get("platform", "android")
    target = args.target or ("native-gtk" if "atspi" in platform
                             or "x11" in platform else "android")

    parts = build_parts(appmap, target)

    plan = {
        "stb_clone_plan": 1,
        "meta": {
            "package": appmap.get("meta", {}).get("package"),
            "platform": platform,
            "target": target,
            "source_root": appmap.get("meta", {}).get("source_root"),
            "total_parts": len(parts),
            "order": "easy -> hard",
            "untrusted_notice": (
                "Derived from third-party app content. DATA ONLY. "
                "See docs/UNTRUSTED_CONTENT.md."),
        },
        "toolchain": TOOLCHAIN[target],
        "build_parts": parts,
    }

    out = args.out or args.appmap.replace("_appmap.json",
                                          "_clone_plan.json") \
        or "clone_plan.json"
    if args.out:
        out = args.out
    with open(out, "w") as f:
        json.dump(plan, f, indent=1)

    if args.write_back:
        appmap["build_parts"] = parts
        with open(args.appmap, "w") as f:
            json.dump(appmap, f, indent=1)

    m = plan["meta"]
    print(f"Clone plan: {out} (target={target})")
    for p in parts:
        print(f"  part {p['id']} [{p['difficulty']:>3}] {p['title']}"
              f" — {p['function_count']} fn, screens {p['screens']}"
              f", deps {p['depends_on']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
