#!/usr/bin/env python3
"""STB dissect: one-command full-flow APK dissection.

  stb-dissect <package> [--apk path] [--source fdroid|aptoide|auto]
             [--max-screens N] [--max-taps N]

Pipeline (all stages skippable when their input already exists):
  1. Acquire APK       F-Droid API / Aptoide API (see acquire.py) or --apk
  2. Install           adb install -r, skip when package already installed
  3. See               traverse the runtime UI into a UI map (JSON + shots)
  4. Think             jadx decompile + bridge UI map to code references
  5. Build             self-contained HTML report
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

sys.path.insert(0, REPO)
from stb.preflight_hook import gate as preflight_gate


def run(cmd, timeout=600):
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return (r.returncode,
            r.stdout.decode(errors="replace"),
            r.stderr.decode(errors="replace"))


def sh(cmd, timeout=600):
    """Print-as-it-happens wrapper for long pipeline stages."""
    print(f"$ {' '.join(cmd)}", flush=True)
    r = subprocess.run(cmd, timeout=timeout)
    if r.returncode != 0:
        print(f"  !! exit {r.returncode}", flush=True)
    return r.returncode


def adb(serial, *args, timeout=60):
    adb_bin = shutil.which("adb") or os.environ.get("STB_ADB", "adb")
    return run([adb_bin, "-s", serial] + list(args), timeout=timeout)


def stage(msg):
    print(f"\n=== {msg} ===", flush=True)


def main():
    ap = argparse.ArgumentParser(
        prog="stb-dissect",
        description="One-command full-flow APK dissection: "
                    "acquire, install, traverse, bridge, report.")
    ap.add_argument("package")
    ap.add_argument("--apk", default=None,
                    help="use this APK instead of downloading")
    ap.add_argument("--source", default="auto",
                    choices=("auto", "fdroid", "aptoide"),
                    help="APK source for the download stage")
    ap.add_argument("--max-screens", type=int, default=15)
    ap.add_argument("--max-taps", type=int, default=40)
    ap.add_argument("--swipe-per-screen", type=int, default=2)
    ap.add_argument("--workdir", default=None,
                    help="run directory (default: STB_RUN_DIR or a new "
                         "stb_run_<pkg> under the cache dir)")
    ap.add_argument("--serial", default=None, help="adb device serial")
    ap.add_argument("--out", default=None, help="report HTML path")
    ap.add_argument("--skip-install", action="store_true")
    args = ap.parse_args()

    serial = args.serial or os.environ.get("STB_ADB_SERIAL") or "localhost:5555"
    t0 = time.time()
    pkg_short = args.package.split(".")[-1]
    work = args.workdir or os.environ.get(
        "STB_RUN_DIR",
        os.path.join(os.path.expanduser("~"),
                     f".hermes/cache/scratch/stb_run_{pkg_short}"))
    os.makedirs(work, exist_ok=True)
    uimap = os.path.join(work, f"uimap_{args.package}.json")
    bridge = uimap.replace(".json", "_bridge.json")
    report = args.out or os.path.join(os.getcwd(), f"{pkg_short}_report.html")

    results = {"package": args.package, "serial": serial, "workdir": work}

    # ---- 1. acquire APK -------------------------------------------------
    apk_path = args.apk
    if apk_path and not os.path.exists(apk_path):
        sys.exit(f"stb-dissect: --apk {apk_path} does not exist")
    if not apk_path:
        stage("1/5 Acquire APK")
        # reuse an existing download when it is still there
        for cand in (f"/tmp/{pkg_short}.apk",
                     os.path.join(work, f"{args.package}.apk")):
            if os.path.exists(cand) and os.path.getsize(cand) > 1_000_000:
                apk_path = cand
                print(f"  reusing {cand}")
                break
        if not apk_path:
            acq = os.path.join(HERE, "acquire.py")
            rc = sh([sys.executable, acq, args.package,
                     "--source", args.source,
                     "--out", os.path.join(work, f"{args.package}.apk")])
            if rc != 0:
                sys.exit("stb-dissect: APK acquisition failed")
            apk_path = os.path.join(work, f"{args.package}.apk")
    results["apk"] = apk_path
    print(f"APK: {apk_path} ({os.path.getsize(apk_path)} bytes)")

    # ---- pre-flight: scan APK + any extracted content before trusting it ----
    allowed, preflight_report = preflight_gate(apk_path, block_on="high")
    results["preflight"] = {
        "allowed": allowed,
        "files_scanned": preflight_report["files_scanned"],
        "findings": preflight_report["findings"],
        "high": preflight_report["high"],
        "medium": preflight_report["medium"],
        "low": preflight_report["low"],
    }
    if not allowed:
        print(f"  stb-dissect: BLOCKED by pre-flight "
              f"({preflight_report['high']} high findings) — aborting")
        sys.exit("stb-dissect: pre-flight blocked APK")
    print(f"  pre-flight: {preflight_report['findings']} finding(s) "
          f"(high {preflight_report['high']}) — proceeding")

    # ---- 2. install ------------------------------------------------------
    if not args.skip_install:
        stage("2/5 Install")
        rc, out, err = adb(serial, "shell", "pm", "list", "packages",
                           args.package, timeout=30)
        if f"package:{args.package}" in out:
            print("  already installed — skipping")
        else:
            rc, out, err = adb(serial, "install", "-r", apk_path,
                               timeout=600)
            print((out + err).strip().splitlines()[-1] if (out + err).strip()
                  else f"exit {rc}")
            if rc != 0:
                sys.exit("stb-dissect: install failed")

    # ---- 3. See (traverse) ------------------------------------------------
    stage("3/5 See — traverse runtime UI")
    rc = sh([sys.executable, os.path.join(HERE, "traverse.py"),
             args.package,
             "--max-screens", str(args.max_screens),
             "--max-taps", str(args.max_taps),
             "--swipe-per-screen", str(args.swipe_per_screen),
             "--out", uimap], timeout=1800)
    if rc != 0 or not os.path.exists(uimap):
        sys.exit("stb-dissect: traversal failed")

    # ---- 4. Think (decompile + bridge) ------------------------------------
    stage("4/5 Think — jadx decompile + bridge")
    dec = os.environ.get(
        "STB_DECOMPILED_DIR",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(
            __file__))), f"decompiled/{pkg_short}"))
    results["decompiled_dir"] = dec
    has_sources = os.path.isdir(dec) and bool(os.listdir(dec)) and (
        os.path.isdir(os.path.join(dec, "sources"))
        or any(os.path.isdir(os.path.join(dec, d))
               for d in os.listdir(dec) if not d.startswith(".")))
    if not has_sources:
        jadx = (shutil.which("jadx") or shutil.which(
            "/opt/jadx/bin/jadx") or "/opt/jadx/bin/jadx")
        rc = sh([jadx, "-d", dec, "--no-res", apk_path], timeout=3600)
        if rc != 0:
            sys.exit("stb-dissect: jadx failed")
    n_java = sum(1 for _, _, fs in os.walk(dec) for f in fs
                 if f.endswith(".java"))
    print(f"  {n_java} java files in {dec}")
    rc = sh([sys.executable, os.path.join(HERE, "bridge.py"), uimap, dec],
            timeout=1800)
    if rc != 0 or not os.path.exists(bridge):
        sys.exit("stb-dissect: bridge failed")

    # ---- 5. Build (report) --------------------------------------------------
    stage("5/5 Build — HTML report")
    rc = sh([sys.executable, os.path.join(HERE, "report.py"),
             "--out", report, uimap, bridge], timeout=600)
    if rc != 0:
        sys.exit("stb-dissect: report failed")

    # ---- summary -----------------------------------------------------------
    try:
        m = json.load(open(uimap))
        b = json.load(open(bridge))
        resolved = sum(1 for s in b["screens"] if s.get("screen_class"))
        refs = sum(len(e.get("evidence") or [])
                   for s in b["screens"] for e in s.get("elements") or [])
        results.update({
            "screens": m.get("screen_count"),
            "taps": m.get("taps_performed"),
            "swipes": m.get("swipes_performed"),
            "bridge_resolved": f"{resolved}/{len(b['screens'])}",
            "code_references": refs,
            "report": report,
        })
    except Exception as exc:            # summary is best-effort
        results["summary_error"] = str(exc)

    results["elapsed_s"] = round(time.time() - t0, 1)
    print("\n=== dissect complete ===")
    print(json.dumps(results, indent=2))
    with open(os.path.join(work, "dissect_summary.json"), "w") as f:
        json.dump(results, f, indent=2)


if __name__ == "__main__":
    main()
