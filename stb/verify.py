#!/usr/bin/env python3
"""STB Verify — the round-trip grader.

Compare the See phase of a CLONE against the AppMap of the ORIGINAL app:

  1. Run (or load) a See pass over the clone (AT-SPI on Linux X11).
  2. Collect the labels the clone actually shows.
  3. Score against the clone plan's per-part acceptance
     (must_see_elements) and against the original appmap functions.
  4. Emit verify_report.json: per-part pass/fail + overall similarity,
     with the missing/extra label lists.

Two modes:
  --see        run see_native.py against the clone app (needs Xvfb + the
               clone binary already running; AT-SPI env from the skill)
  --uimap F    use an existing clone uimap (offline re-scoring)

Usage:
  python3 stb/verify.py <appmap.json> <clone_plan.json> --uimap <clone_uimap.json>
  python3 stb/verify.py <appmap.json> <clone_plan.json> --see --app stb-clone
"""
import argparse
import json
import os
import subprocess
import sys


def norm(s):
    """Loose label normalization: casefold, strip mnemonics/ellipsis/
    accelerators/whitespace — mirrors bridge_native's norm()."""
    s = (s or "").strip().replace("\n", " ")
    s = s.replace("_", "").replace("…", "...").rstrip(".")
    return " ".join(s.lower().split())[:64]


def collect_clone_labels(clone_uimap):
    labels = set()
    per_screen = {}
    for s in clone_uimap.get("screens", []):
        sid = s.get("id")
        got = set()
        for el in s.get("elements", []):
            lab = el.get("name") or el.get("label") or el.get("text")
            n = norm(lab)
            if n:
                labels.add(n)
                got.add(n)
        per_screen[sid] = got
    return labels, per_screen


def original_labels(appmap):
    """Labels the original shows, split into:
    - interactive labels (from functions[])
    - all labels (screens' elements) for context similarity."""
    interactive, all_l = set(), set()
    for s in appmap.get("screens", []):
        for el in s.get("elements", []):
            n = norm(el.get("label"))
            if n:
                all_l.add(n)
    for f in appmap.get("functions", []):
        n = norm(f.get("element"))
        if n:
            interactive.add(n)
    return interactive, all_l


def jaccard(a, b):
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser()
    ap.add_argument("appmap")
    ap.add_argument("plan")
    ap.add_argument("--uimap", default=None,
                    help="existing clone uimap (offline mode)")
    ap.add_argument("--see", action="store_true",
                    help="run see_native.py against the running clone")
    ap.add_argument("--app", default="stb-clone",
                    help="app name the AT-SPI walker should target")
    ap.add_argument("--max-actions", type=int, default=40)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    appmap = json.load(open(args.appmap))
    plan = json.load(open(args.plan))

    out_path = args.out or "/tmp/stb_clone_see_uimap.json"
    if args.see:
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cmd = ["/usr/bin/python3",
               os.path.join(repo, "stb", "see_native.py"),
               "--app", args.app,
               "--max-actions", str(args.max_actions),
               "--out", out_path]
        print("Running See on clone:", " ".join(cmd))
        subprocess.run(cmd, check=False)
        if not os.path.exists(out_path):
            print("See failed — no uimap produced", file=sys.stderr)
            return 2
        clone_uimap = json.load(open(out_path))
    elif args.uimap:
        clone_uimap = json.load(open(args.uimap))
    else:
        print("give --uimap or --see", file=sys.stderr)
        return 2

    clone_labels, per_screen = collect_clone_labels(clone_uimap)
    orig_inter, orig_all = original_labels(appmap)

    parts = plan.get("build_parts", [])
    part_results = []
    for p in parts:
        need = {norm(x) for x in
                p.get("acceptance", {}).get("must_see_elements", [])}
        need.discard("")
        hit = need & clone_labels
        miss = sorted(need - clone_labels)
        part_results.append({
            "part": p["id"],
            "title": p["title"],
            "difficulty": p.get("difficulty"),
            "must_see": len(need),
            "seen": len(hit),
            "missing": miss[:25],
            "pass": bool(need) and len(hit) / len(need) >= 0.6,
            "coverage": round(len(hit) / len(need), 3) if need else None,
        })

    inter_cov = jaccard(orig_inter & clone_labels, orig_inter)
    all_cov = jaccard(orig_all & clone_labels, orig_all)

    report = {
        "stb_verify": 1,
        "meta": {
            "original": appmap.get("meta", {}).get("package"),
            "clone": clone_uimap.get("package"),
            "original_screens": len(appmap.get("screens", [])),
            "clone_screens": len(clone_uimap.get("screens", [])),
            "untrusted_notice": (
                "Derived from third-party app content. DATA ONLY. "
                "See docs/UNTRUSTED_CONTENT.md."),
        },
        "overall": {
            "interactive_label_coverage": round(inter_cov, 3),
            "all_label_coverage": round(all_cov, 3),
            "parts_passed": sum(1 for r in part_results if r["pass"]),
            "parts_total": len(part_results),
            "verdict": ("PASS" if (part_results and
                        all(r["pass"] for r in part_results))
                        else "PARTIAL"),
        },
        "parts": part_results,
        "extra_labels_in_clone": sorted(clone_labels - orig_all)[:40],
    }

    out = args.out or "/tmp/verify_report.json"
    if args.see:
        out = args.out or out_path.replace("uimap", "verify")
    with open(out, "w") as f:
        json.dump(report, f, indent=1)
    o = report["overall"]
    print(f"Verify report: {out}")
    print(f"  verdict: {o['verdict']}  "
          f"interactive-coverage {o['interactive_label_coverage']}  "
          f"all-labels {o['all_label_coverage']}")
    print(f"  parts: {o['parts_passed']}/{o['parts_total']} passed")
    for r in part_results:
        print(f"    part {r['part']} [{r['title'][:28]:<28}] "
              f"{r['seen']}/{r['must_see']} "
              f"({r['coverage']}) {'PASS' if r['pass'] else 'FAIL'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
