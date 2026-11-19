#!/usr/bin/env python3
"""Adapt a canvas_mode uimap JSON into the format stb/bridge.py expects.

canvas_mode screens: {sig, title, elements:[{role, name, pos:[x,y]}], screenshot}
bridge.py wants:     {id, focus, elements:[{text, desc, res, class, x, y}]}

Usage:
    python3 stb/canvas_to_bridge_format.py <canvas_uimap.json> <adapted.json>
    then: stb/bridge.py <adapted.json> <decompiled_dir>
"""

import json
import sys


def adapt(uimap):
    pkg = uimap.get("package", "unknown.pkg")
    screens = []
    for i, s in enumerate(uimap.get("screens", [])):
        elems = []
        for e in s.get("elements", []):
            x, y = (e.get("pos") or [0, 0])
            elems.append({
                "text": e.get("name", "") or "",
                "desc": e.get("name", "") or "",
                "res": "",
                "class": e.get("role", "canvas_tap"),
                "x": int(x) if x is not None else 0,
                "y": int(y) if y is not None else 0,
            })
        screens.append({
            "id": s.get("sig", f"canvas-{i}"),
            "focus": f"Window{{{pkg}/u0 {pkg}/{pkg.split('.')[-1].capitalize()}}}",
            "elements": elems,
        })
    return {
        "package": pkg,
        "platform": "android-canvas",
        "screens": screens,
        "screens_count": len(screens),
        "taps_performed": uimap.get("taps_performed", 0),
        "generated": uimap.get("generated"),
    }


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2:
        print("usage: python3 stb/canvas_to_bridge_format.py <canvas_uimap.json> <adapted.json>")
        return 2
    with open(argv[0]) as f:
        uimap = json.load(f)
    adapted = adapt(uimap)
    with open(argv[1], "w") as f:
        json.dump(adapted, f, indent=2)
    print(f"adapted {len(adapted['screens'])} screens -> {argv[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
