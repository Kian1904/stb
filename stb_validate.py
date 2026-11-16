#!/usr/bin/env python3
"""STB ground-truth validation: compare STB UI map findings against the real
NewPipe source (manifest activities + MainActivity drawer/tabs)."""
import json
import re
import unicodedata

UI_MAP = "/home/agentuser/.hermes/cache/scratch/stb_run/uimap_org.schabi.newpipe.json"
SRC = "/home/agentuser/stb/newpipe-src"
PKG = "org.schabi.newpipe"


def norm(x):
    x = unicodedata.normalize("NFKC", x).replace("\u2019", "'").lower()
    return x.strip()


def main():
    b = json.load(open(UI_MAP))

    # collect all STB-seen texts
    stb = set()
    for s in b["screens"]:
        for e in s["elements"]:
            t = (e["text"] or e["desc"] or "").strip()
            if t:
                stb.add(t)
    stb_n = {norm(x) for x in stb}

    print("=" * 60)
    print("GROUND TRUTH 1: Manifest activities")
    print("=" * 60)
    m = open(f"{SRC}/app/src/main/AndroidManifest.xml").read()
    acts = set(re.findall(r'<activity[^>]*android:name="([^"]+)"', m))
    norm_acts = set()
    for a in acts:
        if a.startswith("."):
            norm_acts.add(PKG + a)
        elif a.startswith(PKG):
            norm_acts.add(a)
        else:
            norm_acts.add(PKG + "." + a)

    visited = set()
    ok = 0
    drift = 0
    for s in b["screens"]:
        comp = s["focus"]
        if "/" in comp:
            cpkg, ccls = comp.split("/", 1)
        else:
            cpkg, ccls = None, comp
        if cpkg == PKG:
            # component class may be fully qualified or relative
            if ccls.startswith(PKG):
                cname = PKG + ccls[len(PKG):]
            else:
                cname = PKG + "." + ccls.split(".")[-1]
            inman = cname in norm_acts or PKG + "." + ccls.split(".")[-1] in norm_acts
            visited.add(cname)
            if inman:
                ok += 1
                print(f"  screen {s['id']}: {ccls.split('.')[-1]} -> MANIFEST-OK")
            else:
                print(f"  screen {s['id']}: {ccls} -> CHECK")
        else:
            drift += 1
            print(f"  screen {s['id']}: {comp[:55]} -> (other app / drift)")
    never = {a for a in norm_acts if a.split(".")[-1] not in {v.split(".")[-1] for v in visited}}
    print(f"\n  Verified in-manifest screens: {ok}")
    print(f"  Drift screens (other apps): {drift}")
    print(f"  Manifest activities NOT visited ({len(never)}):")
    for a in sorted(never):
        print(f"    - {a.split('.')[-1]}")

    print()
    print("=" * 60)
    print("GROUND TRUTH 2: Drawer/tab items (MainActivity + strings.xml)")
    print("=" * 60)
    # items known to exist in the drawer from MainActivity.java lines ~280-318
    gt_items = ["Subscriptions", "What's New", "Bookmarked Playlists",
                "Downloads", "History", "Settings", "About", "Live", "Search",
                "Open Drawer"]
    seen = 0
    for k in gt_items:
        hit = norm(k) in stb_n or any(norm(k) in x for x in stb_n)
        seen += hit
        print(f"  {'✓ seen' if hit else '✗ missed'}: {k}")
    print(f"\n  Drawer item coverage: {seen}/{len(gt_items)}")

    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Screens mapped: {len(b['screens'])}")
    print(f"  In-manifest verification: {ok} screens correct")
    print(f"  Known UI items seen: {seen}/{len(gt_items)}")
    print(f"  Known gaps: {drift} drift screens; deep screens (Settings, About,")
    print(f"  Player) need targeted taps — expected for 40-tap budget MVP")


if __name__ == "__main__":
    main()
