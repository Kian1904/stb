#!/usr/bin/env python3
"""STB pre-flight hook — gate pipeline steps on untrusted-content scan.

Wraps stb.preflight.gate so any STB sub-command that consumes third-party
content (skill markdown, plugin, APK, decompiled code, Dart string pool)
can refuse to proceed when HIGH-severity injection-shaped strings are found.

Usage as CLI:
    python3 stb/preflight_hook.py <path> [--block-on high|medium|low]
    exit 0 = clean, exit 1 = blocked

Usage inside a pipeline script:
    from stb.preflight_hook import gate
    allowed, report = gate("path/to/skill-or-plugin")
    if not allowed: abort(...)
"""

import json
import os
import sys

from stb.sanitize import scan_tree, summarize

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}


def gate(path, block_on="high", max_files=400):
    """Scan a path. Returns (allowed, report_dict).

    allowed is False when any finding at or above block_on severity exists.
    """
    results = scan_tree(path, max_files=max_files)
    report = summarize(results)
    threshold = SEVERITY_ORDER.get(block_on, 2)
    blocked = [
        f for r in report["results"] for f in r["findings"]
        if SEVERITY_ORDER.get(f.get("severity", "low"), 0) >= threshold
    ]
    allowed = len(blocked) == 0
    report["blocked_findings"] = blocked
    report["block_on"] = block_on
    return allowed, report


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: python3 stb/preflight_hook.py <path> [--block-on high|medium|low]")
        print("  exit 0 = clean, exit 1 = blocked")
        return 0
    target = argv[0]
    block_on = "high"
    if "--block-on" in argv:
        i = argv.index("--block-on")
        if i + 1 < len(argv):
            block_on = argv[i + 1]
    if not os.path.exists(target):
        print(f"stb-preflight-hook: not found: {target}", file=sys.stderr)
        return 2
    allowed, report = gate(target, block_on=block_on)
    print(f"scanned {report['files_scanned']} file(s), "
          f"{report['files_flagged']} flagged, {report['findings']} finding(s) "
          f"(high {report['high']} / med {report['medium']} / low {report['low']}) "
          f"[block_on={block_on}]")
    for r in report["results"]:
        if not r["findings"]:
            continue
        print(f"\n{r['path']}  [{r['kind']}]")
        for f in r["findings"]:
            loc = f":{f['line']}" if f.get("line") else ""
            print(f"  [{f['severity']}] {f['pattern']}{loc} — {f['description']}")
            print(f"      {f['match'][:110]}")
    if not allowed:
        print(f"\nBLOCKED: findings at {block_on} or higher present. Do not proceed.")
    return 1 if not allowed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] if len(sys.argv) > 1 else None))
