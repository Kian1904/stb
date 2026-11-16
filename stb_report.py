#!/usr/bin/env python3
"""STB Build-phase: generate a self-contained HTML report from the UI map
and the Think-phase bridge (screenshot embedded as base64).

Usage: python3 stb_report.py <uimap.json> <bridge.json> [--out report.html]
"""
import argparse
import base64
import html
import json
import os


def img_b64(path):
    if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def esc(s):
    return html.escape(str(s)) if s else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("uimap")
    ap.add_argument("bridge")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    with open(args.uimap) as f:
        uimap = json.load(f)
    with open(args.bridge) as f:
        bridge = json.load(f)

    out_path = args.out or args.uimap.replace(".json", "_report.html")
    pkg = uimap["package"]

    # index screens by id for merging
    bmap = {s["id"]: s for s in bridge["screens"]}

    cards = []
    for s in uimap["screens"]:
        b = bmap.get(s["id"], {})
        shot = img_b64(s.get("screenshot"))
        shot_html = (
            f'<img src="data:image/png;base64,{shot}" alt="screen {s["id"]}">'
            if shot
            else '<div class="blocked">🔒 screenshot diblokir (FLAG_SECURE) — gunakan UI dump</div>'
        )
        screen_class = esc(b.get("screen_class") or "—")

        rows = []
        for el, bel in zip(s["elements"], b.get("elements", [])):
            evs = bel.get("evidence", [])
            ev_html = ""
            if evs:
                links = []
                for ev in evs[:3]:
                    if ev["via"] == "layout-decl":
                        # layouts list entry (no file) — render as chip
                        links.append(
                            f'<code class="ev lay" title="declared in layout XMLs">'
                            f'⚙ {esc(", ".join(ev.get("layouts", [])[:3]))}</code>'
                        )
                        continue
                    badge = {"layout": "🧩 "}.get(ev["via"], "")
                    links.append(
                        f'<code class="ev {esc(ev["via"])}" title="via {esc(ev["via"])}: {esc(ev["pattern"])}">'
                        f'{badge}{esc(ev["file"])}</code>'
                    )
                ev_html = "".join(links)
            else:
                ev_html = '<span class="noev">no direct evidence</span>'
            label = el["text"] or el["desc"] or el["res"] or el["class"]
            pos = "({},{})".format(el["x"], el["y"])
            rows.append(
                f'<tr><td class="lbl">{esc(label[:40])}</td>'
                f'<td>{esc(el["class"].split(".")[-1])}</td>'
                f'<td>{esc(pos)}</td>'
                f'<td class="code">{ev_html}</td></tr>'
            )

        cards.append(f"""
<div class="card" id="s{s['id']}">
  <div class="head">
    <span class="sid">Screen {s['id']}</span>
    <code class="focus">{esc(s['focus'])}</code>
    <span class="impl">implements: <code>{screen_class}</code></span>
  </div>
  <div class="body">
    <div class="shot">{shot_html}</div>
    <table class="els">
      <tr><th>element</th><th>class</th><th>pos</th><th>code evidence</th></tr>
      {''.join(rows)}
    </table>
  </div>
</div>""")

    html_doc = f"""<!DOCTYPE html>
<html lang="id"><head><meta charset="utf-8">
<title>STB UI Map — {esc(pkg)}</title>
<style>
  :root {{ --bg:#0d1117; --card:#161b22; --fg:#c9d1d9; --accent:#58a6ff; --mut:#8b949e; }}
  body {{ background:var(--bg); color:var(--fg); font-family:ui-sans-serif,system-ui,sans-serif; margin:2rem; }}
  h1 {{ color:var(--accent); }} .meta {{ color:var(--mut); margin-bottom:2rem; }}
  .card {{ background:var(--card); border:1px solid #30363d; border-radius:10px; margin-bottom:1.5rem; overflow:hidden; }}
  .head {{ padding:.8rem 1rem; border-bottom:1px solid #30363d; display:flex; gap:1rem; align-items:center; flex-wrap:wrap; }}
  .sid {{ background:var(--accent); color:#000; font-weight:700; padding:.15rem .6rem; border-radius:6px; }}
  .focus {{ color:var(--mut); font-size:.85em; }} .impl {{ font-size:.85em; color:var(--mut); }}
  .impl code {{ color:#7ee787; }}
  .body {{ display:flex; gap:1rem; padding:1rem; flex-wrap:wrap; }}
  .shot img {{ max-width:280px; border-radius:8px; border:1px solid #30363d; }}
  .blocked {{ width:280px; padding:3rem 1rem; text-align:center; color:var(--mut); border:1px dashed #30363d; border-radius:8px; }}
  .els {{ border-collapse:collapse; flex:1; min-width:380px; font-size:.9em; }}
  .els th {{ text-align:left; color:var(--mut); border-bottom:1px solid #30363d; padding:.3rem .5rem; }}
  .els td {{ padding:.3rem .5rem; border-bottom:1px solid #21262d; vertical-align:top; }}
  .lbl {{ font-weight:600; }} .code code.ev {{ display:block; color:#7ee787; font-size:.85em; background:none; }}
  .noev {{ color:#484f58; font-style:italic; }}
  .toc a {{ color:var(--accent); text-decoration:none; margin-right:.8rem; }}
</style></head><body>
<h1>STB UI Map — {esc(pkg)}</h1>
<div class="meta">
  {uimap['screen_count']} screens · {uimap['taps_performed']} taps · generated {esc(uimap['generated'])}
  · See: runtime traversal · Think: jadx evidence bridge
</div>
<div class="toc">{"".join(f'<a href="#s{s["id"]}">#{s["id"]}</a>' for s in uimap['screens'])}</div>
{''.join(cards)}
</body></html>"""

    with open(out_path, "w") as f:
        f.write(html_doc)
    size = os.path.getsize(out_path)
    print(f"Report: {out_path} ({size/1024:.0f} KB)")

if __name__ == "__main__":
    main()
