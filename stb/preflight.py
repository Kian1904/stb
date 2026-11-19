#!/usr/bin/env python3
"""STB pre-flight: scan third-party skills/plugins/markdown BEFORE loading.

STB eats untrusted content by design. Any skill/plugin markdown can carry
an indirect prompt injection aimed at the agent reading it. This module is
the enforcement point:

1. scan_skill_dir(path)   — run sanitize.scan_tree on a skill/plugin dir.
2. gate(path)             — return (allowed, report). allowed=False if any
   HIGH severity finding exists.
3. main(argv)             — CLI: python3 stb/preflight.py <skill-dir>
   (exit 1 = blocked, exit 0 = clean).

Usage in a loader:
    from stb.preflight import gate
    allowed, report = gate("~/.hermes/skills/some-skill")
    if not allowed: abort load, show report to user
"""

import json
import os
import sys

from stb.sanitize import scan_tree, summarize


def gate(path, max_files=400):
    """Scan a skill/plugin dir. Returns (allowed, report_dict).

    allowed is False when any HIGH severity finding exists — those are
    injection-shaped strings that must not reach agent context without
    explicit human review.
    """
    results = scan_tree(path, max_files=max_files)
    report = summarize(results)
    high = report["high"]
    allowed = high == 0
    return allowed, report


def main(argv=None):
    argv = list(argv or [])
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: python3 stb/preflight.py <skill-or-plugin-dir>")
        print("  exit 0 = clean (no HIGH findings), exit 1 = blocked")
        return 0
    target = argv[0]
    if not os.path.exists(target):
        print(f"stb-preflight: not found: {target}", file=sys.stderr)
        return 2
    allowed, report = gate(target)
    print(f"scanned {report['files_scanned']} file(s), "
          f"{report['files_flagged']} flagged, {report['findings']} finding(s) "
          f"(high {report['high']} / med {report['medium']} / low {report['low']})")
    for r in report["results"]:
        if not r["findings"]:
            continue
        print(f"\n{r['path']}  [{r['kind']}]")
        for f in r["findings"]:
            loc = f":{f['line']}" if f.get("line") else ""
            print(f"  [{f['severity']}] {f['pattern']}{loc} — {f['description']}")
            print(f"      {f['match'][:110]}")
    if not allowed:
        print("\nBLOCKED: HIGH-severity findings present. Do not load this skill/plugin.")
    return 1 if not allowed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] if len(sys.argv) > 1 else None))
