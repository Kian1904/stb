#!/usr/bin/env python3
"""STB Think-phase bridge: connect UI-map elements to decompiled code.

For each element in the UI map JSON, search the jadx-decompiled sources for
matching identifiers (resource-id, text, content-desc) and record the files
that likely implement the element's behavior.

Usage: python3 stb_bridge.py <uimap.json> <decompiled_dir> [--out bridge.json]
"""
import argparse
import json
import os
import re
import shutil
import subprocess


def search_code(decompiled_dir, pattern, is_regex=True, max_files=8):
    """Search decompiled sources; ripgrep if available, else pure Python."""
    rg = shutil.which("rg")
    if rg:
        cmd = [rg, "--no-heading", "-l", "--glob", "*.java"]
        if not is_regex:
            cmd.append("-F")
        cmd += [pattern, decompiled_dir]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=60)
            return r.stdout.decode(errors="replace").splitlines()[:max_files]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    # portable fallback: walk the tree in Python (works on Windows/macOS/Linux)
    rx = None
    if is_regex:
        try:
            rx = re.compile(pattern)
        except re.error:
            is_regex = False
    hits = []
    for root, _dirs, files in os.walk(decompiled_dir):
        for fn in files:
            if not fn.endswith(".java"):
                continue
            fp = os.path.join(root, fn)
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                    data = fh.read()
            except OSError:
                continue
            found = rx.search(data) if (is_regex and rx) else (pattern in data)
            if found:
                hits.append(fp)
                if len(hits) >= max_files:
                    return hits
    return hits

def suggest_class_for_screen(focus_component, decompiled_dir):
    """Find the Activity/Fragment class file for a focused component."""
    comp = focus_component.replace("Window{", "").rstrip("}")
    if "/" not in comp:
        return None, []
    pkg, cls = comp.split("/", 1)
    path = os.path.join(decompiled_dir, "sources", pkg.replace(".", "/"), cls + ".java")
    exists = os.path.exists(path)
    return (path if exists else None), []

def find_layout_bindings(decompiled_dir, res_id_name, max_hits=6):
    """Chain: res-id name -> layout XML that declares it -> code that
    inflates that layout (via R.layout.*) -> binding class if present.
    Only works when jadx was run WITH resources (no --no-res)."""
    res_dir = os.path.join(decompiled_dir, "resources", "res")
    if not os.path.isdir(res_dir):
        return None
    layouts = []
    rx = re.compile(r'@\+?id/' + re.escape(res_id_name) + r'\b')
    for root, _dirs, files in os.walk(res_dir):
        bn = os.path.basename(root)
        if bn != "layout" and bn != "menu":
            continue
        for fn in files:
            if not fn.endswith(".xml"):
                continue
            fp = os.path.join(root, fn)
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                    data = fh.read()
            except OSError:
                continue
            if rx.search(data):
                layouts.append(os.path.splitext(fn)[0])
                if len(layouts) >= max_hits:
                    break
        if len(layouts) >= max_hits:
            break
    return layouts or None


def bridge_element(elem, decompiled_dir):
    """Search decompiled code for evidence related to a UI element."""
    evidence = []
    seen = set()

    candidates = []
    if elem.get("res"):
        candidates.append(("resource-id", elem["res"]))
    if elem.get("desc"):
        candidates.append(("content-desc", elem["desc"]))
    if elem.get("text") and len(elem["text"]) > 2:
        candidates.append(("text", elem["text"]))

    for kind, pat in candidates:
        if kind == "resource-id":
            # resource id like 'btnNext' → R.id.btnNext in code
            hits = search_code(decompiled_dir, f"R.id.{pat}", is_regex=False, max_files=5)
            # layout chain: which layout XML declares this id?
            layouts = find_layout_bindings(decompiled_dir, pat)
            if layouts:
                for lay in layouts[:3]:
                    laname = f"R.layout.{lay}"
                    lhits = search_code(decompiled_dir, laname,
                                        is_regex=False, max_files=2)
                    for h in lhits:
                        if h not in seen:
                            seen.add(h)
                            evidence.append({"via": "layout", "pattern": laname,
                                             "file": h.replace(decompiled_dir + os.sep, "")})
                evidence.append({"via": "layout-decl", "pattern": pat,
                                 "layouts": layouts[:5]})
        else:
            # escape quotes for fixed-string search
            hits = search_code(decompiled_dir, pat, is_regex=False, max_files=5)
        for h in hits:
            if h not in seen:
                seen.add(h)
                evidence.append({"via": kind, "pattern": pat, "file": h.replace(decompiled_dir + "/", "")})
    return evidence

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("uimap")
    ap.add_argument("decompiled_dir")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    with open(args.uimap) as f:
        uimap = json.load(f)

    result = {
        "package": uimap["package"],
        "decompiled_dir": args.decompiled_dir,
        "screens": [],
    }

    for s in uimap["screens"]:
        screen_entry = {
            "id": s["id"],
            "focus": s["focus"],
            "screen_class": None,
            "elements": [],
        }
        # screen-level: which class implements this activity/fragment?
        comp = s["focus"]
        if comp.startswith("Window{"):
            comp = comp.split("u0 ")[-1].rstrip("}")
        pkg_cls = comp.split(":")[-1]  # drop window suffixes
        if "/" in pkg_cls:
            pkg, cls = pkg_cls.split("/", 1)
            # handle the short form com.pkg/.MainActivity
            if cls.startswith("."):
                cls = pkg.split(".")[-1] + cls
            # handle fully-qualified class in component (com.pkg/com.pkg.MainActivity):
            # keep the full class fqcn — its own package dirs give the source path
            if cls == pkg:
                cls = cls.split(".")[-1]
            # try full package, then progressively shorter prefixes AND
            # prefixes derived from the class package itself (applicationId
            # suffix like _tp often differs from the code package)
            # if the class is fully qualified and NOT under the applicationId,
            # its own package IS the source path root
            if not cls.startswith(pkg + "."):
                candidates = [cls.rsplit(".", 1)[0], pkg]
            else:
                # class lives in a sub-package of the applicationId, e.g.
                # com.spotify.lite/.../welcome.WelcomeActivity → sources/
                # com/spotify/lite/features/welcome/WelcomeActivity.java
                candidates = [cls.rsplit(".", 1)[0], pkg]
            parts = pkg.split(".")
            for cut in range(1, len(parts) + 1):
                candidates.append(".".join(parts[:len(parts) - cut]))
            seen_paths = set()
            simple = cls.rsplit(".", 1)[-1]
            for trial_pkg in candidates:
                if not trial_pkg:
                    continue
                path = os.path.join("sources", trial_pkg.replace(".", "/"),
                                    simple + ".java")
                if path in seen_paths:
                    continue
                seen_paths.add(path)
                if os.path.exists(os.path.join(args.decompiled_dir, path)):
                    screen_entry["screen_class"] = path
                    break
        for e in s["elements"]:
            ev = bridge_element(e, args.decompiled_dir)
            screen_entry["elements"].append({
                "label": e["text"] or e["desc"] or e["res"] or e["class"],
                "class": e["class"],
                "x": e["x"], "y": e["y"],
                "evidence": ev[:4],
            })
        result["screens"].append(screen_entry)

    out_path = args.out or args.uimap.replace(".json", "_bridge.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    # summary
    total_ev = sum(len(el["evidence"]) for s in result["screens"] for el in s["elements"])
    print(f"Bridged {len(result['screens'])} screens, {total_ev} code references found")
    print(f"Output: {out_path}")

if __name__ == "__main__":
    main()
