#!/usr/bin/env python3
"""STB acquire: download an APK for a package from a public source.

  stb-acquire <package> [--source auto|fdroid|aptoide] [--out path.apk]

Sources:
- fdroid   F-Droid official repo API (open-source apps only)
- aptoide  Aptoide public ws75 API (works for Play-Store-only apps; beware
           stale third-party uploads — check the version before trusting)
- auto     try F-Droid first, then Aptoide

Both APIs are plain HTTPS GET with no bot protection (unlike APKPure /
APKMirror web pages, which are Cloudflare-blocked from servers).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like "
      "Gecko) Chrome/120 Safari/537.36")


def http_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fdroid_url(pkg):
    """Latest versionCode from the F-Droid package API."""
    d = http_json(f"https://f-droid.org/api/v1/packages/{pkg}")
    vc = d.get("suggestedVersionCode")
    if not vc:
        pkgs = d.get("packages") or []
        if not pkgs:
            return None
        vc = pkgs[0].get("versioncode")
    return f"https://f-droid.org/repo/{pkg}_{vc}.apk"


def aptoide_url(pkg):
    """Direct pool APK path from the Aptoide ws75 app API."""
    d = http_json(f"https://ws75.aptoide.com/api/7/app/get/package_name={pkg}")
    meta = (d.get("nodes") or {}).get("meta") or {}
    data = meta.get("data") or {}
    f = data.get("file") or {}
    return f.get("path")


def download(url, out_path):
    print(f"  GET {url}")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=300) as r, \
            open(out_path, "wb") as f:
        shutil.copyfileobj(r, f)
    size = os.path.getsize(out_path)
    # sanity: an APK is a ZIP; guard against error pages saved as files
    if size < 1_000_000 or open(out_path, "rb").read(2) != b"PK":
        os.remove(out_path)
        return None
    return size


def main():
    ap = argparse.ArgumentParser(prog="stb-acquire")
    ap.add_argument("package")
    ap.add_argument("--source", default="auto",
                    choices=("auto", "fdroid", "aptoide"))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    out = args.out or f"/tmp/{args.package.split('.')[-1]}.apk"
    tried = []
    if args.source in ("auto", "fdroid"):
        tried.append("fdroid")
        try:
            url = fdroid_url(args.package)
            if url:
                size = download(url, out)
                if size:
                    print(f"OK {out} ({size} bytes, from F-Droid)")
                    return
        except Exception as exc:
            print(f"  fdroid: {exc}")
    if args.source in ("auto", "aptoide"):
        tried.append("aptoide")
        try:
            url = aptoide_url(args.package)
            if url:
                size = download(url, out)
                if size:
                    print(f"OK {out} ({size} bytes, from Aptoide)")
                    return
        except Exception as exc:
            print(f"  aptoide: {exc}")
    sys.exit(f"stb-acquire: no APK obtained for {args.package} "
             f"(tried: {', '.join(tried)})")


if __name__ == "__main__":
    main()
