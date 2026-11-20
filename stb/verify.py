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
import re
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
            if el.get("role") == "page tab" and lab:
                # Clone tabs are titled "Screen N: <original title>" — also
                # register the bare title so must_see entries match.
                title = lab.split(": ", 1)[1] if ": " in lab else lab
                n2 = norm(title)
                if n2:
                    labels.add(n2)
                    got.add(n2)
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


# --------------------------------------------------------------------------
# Behavior-aware verification
# --------------------------------------------------------------------------
#
# Labels say "the clone SHOWS the element". Behavior says "the clone DOES
# what the original does". Expected behaviors are derived from the ORIGINAL
# appmap's flows; observed behaviors from the CLONE's traversal log, which
# records every tap and every "-> new screen" state change.
#
# Behavior classes:
#   navigation  : a flow trigger label produced a new screen in the clone
#   interactive : a trigger label was tapped without a screen change (valid
#                 for in-place actions — dialogs, toggles, calendar months
#                 rendered as one Activity)
#   untapped    : trigger label never exercised during the clone traversal



def _parse_tap_entry(line):
    """'[tap 2/50] menu item 'New' @ (5, 83)' -> ('New',).
    Backslash-free on purpose: regex literals get mangled by some
    tooling pipelines, plain string ops never do."""
    if not line.startswith("[tap ") or "]" not in line:
        return None
    # Both formats exist:
    #   android: [tap 1/12] tap android.widget.Button 'LABEL' @ (x,y)
    #   native : [tap 2/50] menu item 'New' @ (5, 83)
    # Either way, the payload is everything after the first "] ".
    rest = line.split("] ", 1)[1]
    q1 = rest.find("'")
    if q1 < 0:
        return None
    q2 = rest.find("'", q1 + 1)
    if q2 < 0:
        return None
    label = rest[q1 + 1:q2].strip()
    return label or None


def observed_behaviors(clone_uimap, log_text):
    """Parse the clone traversal log into {normalized_label: behavior}."""
    behaviors = {}
    if not log_text:
        return behaviors
    lines = log_text.splitlines()
    for idx, line in enumerate(lines):
        label = _parse_tap_entry(line)
        if not label:
            continue
        lab = norm(label)
        if not lab:
            continue
        nxt = ""
        for cand in lines[idx + 1:idx + 4]:
            if cand.strip():
                nxt = cand
                break
        if "-> new screen " in nxt:
            # navigation wins: a tab tapped while already selected reads as
            # 'interactive' on the first tap, but the same tab navigates
            # when tapped from another page — that is its real behavior.
            behaviors[lab] = "navigation"
        else:
            behaviors.setdefault(lab, "interactive")
    return behaviors


def expected_behaviors(appmap):
    """From the ORIGINAL appmap flows: trigger label -> 'navigation'."""
    exp = {}
    for f in appmap.get("flows", []):
        t = f.get("trigger") or {}
        lab = norm(t.get("label"))
        if lab:
            exp.setdefault(lab, "navigation")
    return exp


def score_behaviors(expected, observed):
    """Returns (checked, passed, missed, details[])."""
    checked, passed, missed, details = 0, 0, [], []
    for lab, kind in sorted(expected.items()):
        checked += 1
        obs = observed.get(lab)
        if obs is None:
            # Tab labels in clones may carry a "Screen N: <title>" prefix —
            # match on the title part if any observed label ends with it.
            # Prefer the LONGEST prefix (page-tab navigation) over a bare
            # endswith, so a combo box "All Output Devices" cannot shadow the
            # page tab "Output Devices".
            best = None
            for olab, okind in observed.items():
                parts = olab.split(": ")
                if len(parts) >= 2 and parts[-1] == lab:
                    # tab-prefix match ("Screen 2: Output Devices")
                    best = okind
                    break
                if olab == lab:
                    best = okind
            if best is None:
                for olab, okind in observed.items():
                    if olab.endswith(lab):
                        best = okind
                        break
            obs = best
        if obs == kind:          # navigation expected AND observed
            passed += 1
            details.append({"label": lab, "expected": kind,
                            "observed": obs, "ok": True})
        elif obs:                 # tapped, but no screen change
            missed.append(lab)
            details.append({"label": lab, "expected": kind,
                            "observed": obs, "ok": False})
        else:                     # never exercised
            missed.append(lab)
            details.append({"label": lab, "expected": kind,
                            "observed": None, "ok": False})
    return checked, passed, missed, details


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
    ap.add_argument("--traversal-log", default=None,
                    help="clone traversal log for behavior-aware scoring "
                         "(default: <uimap>_traversal.log next to --uimap)")
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

    log_path = args.traversal_log
    if not log_path and args.uimap:
        cand = args.uimap.rstrip(".json") + "_traversal.log"
        cand2 = args.uimap.replace(".json", "") + "_traversal.log"
        log_path = cand if os.path.exists(cand) else (
            cand2 if os.path.exists(cand2) else None)
    log_text = ""
    if log_path and os.path.exists(log_path):
        log_text = open(log_path, errors="replace").read()

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

    exp_beh = expected_behaviors(appmap)
    obs_beh = observed_behaviors(clone_uimap, log_text)
    beh_checked, beh_passed, beh_missed, beh_details = \
        score_behaviors(exp_beh, obs_beh)
    beh_cov = round(beh_passed / beh_checked, 3) if beh_checked else None

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
            "behavior_coverage": beh_cov,
            "behaviors_checked": beh_checked,
            "behaviors_passed": beh_passed,
            "behaviors_missing": beh_missed[:25],
            "parts_passed": sum(1 for r in part_results if r["pass"]),
            "parts_total": len(part_results),
            "verdict": ("PASS" if (part_results
                        and all(r["pass"] for r in part_results)
                        and (beh_checked == 0
                             or beh_passed / beh_checked >= 0.6))
                        else "PARTIAL"),
        },
        "parts": part_results,
        "behaviors": beh_details,
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
    if beh_checked:
        print(f"  behaviors: {beh_passed}/{beh_checked} "
              f"(coverage {beh_cov})")
        for d in beh_details:
            mark = "OK " if d["ok"] else "MISS"
            obs = d["observed"] or "never-exercised"
            print(f"    [{mark}] {d['label'][:40]:<40} "
                  f"expected={d['expected']} observed={obs}")
    for r in part_results:
        print(f"    part {r['part']} [{r['title'][:28]:<28}] "
              f"{r['seen']}/{r['must_see']} "
              f"({r['coverage']}) {'PASS' if r['pass'] else 'FAIL'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
