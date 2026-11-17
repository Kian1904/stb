#!/usr/bin/env python3
"""STB Think phase for native desktop apps: bridge an AT-SPI UI map to code.

The Linux counterpart of stb_bridge.py (Android/jadx). Takes the uimap
produced by stb_see_native.py and resolves every named UI element to
evidence in source code:

  1. app source tree (--source dir): .ui menu/builder files (label,
     tooltip attributes) and .c/.h/_() gettext strings, including
     multi-line string concatenation.
  2. external dependency trees (--ext-dep, repeatable): e.g.
     /usr/share/gtksourceview-4/language-specs (.lang display names),
     /usr/share/gtksourceview-4/styles. Elements resolved here are
     classified `external-dep` — real evidence, different provenance.
  3. known runtime-generated labels (--stock): GTK stock buttons
     (Yes/No/OK/Cancel...), numbers from stateful actions. Classified
     `runtime-generated`.

Unresolved elements stay `unresolved` — never guessed.

Usage:
  python3 stb_bridge_native.py <uimap.json> --source /path/to/src \
      [--ext-dep /usr/share/gtksourceview-4/language-specs] \
      [--out bridge.json]

Output JSON layout mirrors stb_bridge.py: screens[] each carrying
elements[] with evidence[{via, pattern, file, kind}].
"""
import argparse
import json
import os
import re
import sys

MNEMONIC = re.compile(r"_+(?=[A-Za-z0-9])")  # GTK _File / New _Window
LANG_NAME = re.compile(r'<language\s+[^>]*?_?name="([^"]+)"')


def norm(s):
    """Normalize a UI label for matching: strip mnemonics, ellipses, case."""
    s = MNEMONIC.sub("", s)
    s = s.replace("…", "...").strip()
    if s.endswith("..."):
        s = s[:-3]
    return s.strip().lower()


class SourceIndex:
    def __init__(self, root, exts=(".ui", ".c", ".h", ".cc", ".cpp", ".py",
                                   ".js", ".rs", ".go")):
        self.root = root
        self.exts = exts
        self.label_map = {}  # normalized label -> [(file, raw_label)]
        self._build()

    def _add(self, label, path):
        if not label or len(label) > 120:
            return
        key = norm(label)
        if key:
            self.label_map.setdefault(key, []).append(
                (os.path.relpath(path, self.root), label))

    def _build(self):
        # attributes with optional translatable="yes|no": covers both
        # <attribute name="label" translatable="yes">X</attribute> and
        # <attribute name="label">X</attribute>
        ui_attr = re.compile(
            r'(\w+)="[^"]*">([^<]{1,120})</attribute>')
        gettext = re.compile(r'_\(((?:"[^"]*"\s*)+)\)')  # incl. concatenation
        strlit = re.compile(r'"([^"]*)"')
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames
                           if d not in (".git", "node_modules", "__pycache__",
                                        "po", "build")]
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in self.exts:
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    txt = open(path, errors="replace").read()
                except OSError:
                    continue
                if ext == ".ui":
                    for m in ui_attr.finditer(txt):
                        self._add(m.group(2), path)
                elif ext in (".c", ".h", ".cc", ".cpp"):
                    for m in gettext.finditer(txt):
                        parts = strlit.findall(m.group(1))
                        self._add("".join(parts), path)
                elif ext in (".py", ".js", ".rs", ".go"):
                    for m in re.finditer(r'_?\("([^"]{1,120})"\)', txt):
                        self._add(m.group(1), path)

    def lookup(self, name):
        hits = self.label_map.get(norm(name))
        if not hits:
            return None
        return [{
            "via": "source-string",
            "pattern": raw,
            "file": f"{self.root}/{rel}" if not os.path.isabs(self.root)
                    else os.path.join(self.root, rel),
            "kind": os.path.splitext(rel)[1] or "src",
        } for rel, raw in hits[:3]]


class ExtDepIndex(SourceIndex):
    """Index for external dependency trees (e.g. gtksourceview)."""

    def __init__(self, root, kind="ext-dep"):
        super().__init__(root, exts=(".ui", ".c", ".h", ".xml", ".lang",
                                     ".py", ".js", ".rs", ".go"))
        self.kind = kind
        for fn in os.listdir(root) if os.path.isdir(root) else []:
            path = os.path.join(root, fn)
            if not os.path.isfile(path):
                continue
            txt = open(path, errors="replace").read()
            # gtksourceview .lang: <language id="html" name="HTML" _section="Markup">
            if fn.endswith(".lang"):
                m = LANG_NAME.search(txt)
                if m:
                    self._add(m.group(1), path)
                for m2 in re.finditer(r'_section="([^"]+)"', txt):
                    self._add(m2.group(1), path)
            # gtksourceview .xml style schemes: <style-scheme id="kate" name="Kate">
            elif fn.endswith(".xml"):
                m = re.search(r'<style-scheme[^>]*name="([^"]+)"', txt)
                if m:
                    self._add(m.group(1), path)

    def lookup(self, name):
        hits = self.label_map.get(norm(name))
        if not hits:
            return None
        return [{
            "via": self.kind,
            "pattern": raw,
            "file": os.path.join(self.root, rel),
            "kind": self.kind,
        } for rel, raw in hits[:2]]


# Labels the toolkit itself provides at runtime (GTK stock / stateful
# actions) — evidence lives in the toolkit, not the app source.
GTK_STOCK = {
    "yes", "no", "ok", "cancel", "close", "open", "save", "apply", "quit",
    "question", "warning", "error", "info",
}


def bridge(uimap, source, ext_deps, out_path):
    idx = SourceIndex(source)
    exts = [ExtDepIndex(d) for d in ext_deps or []]
    screens_out = []
    stats = {"source": 0, "external-dep": 0, "runtime-generated": 0,
             "unresolved": 0, "unnamed": 0}

    for s in uimap.get("screens", []):
        els_out = []
        for e in s.get("elements", []):
            name = (e.get("name") or "").strip()
            entry = {"label": name or None,
                     "class": e.get("role", ""),
                     "x": (e.get("pos") or [None])[0],
                     "y": (e.get("pos") or [None, None])[1],
                     "evidence": []}
            if not name:
                stats["unnamed"] += 1
                els_out.append(entry)
                continue

            if name.lower() in GTK_STOCK:
                entry["evidence"].append({
                    "via": "runtime-generated",
                    "pattern": name,
                    "file": "gtk-stock",
                    "kind": "stock-widget",
                })
                stats["runtime-generated"] += 1
            else:
                ev = idx.lookup(name)
                if ev:
                    entry["evidence"] = ev
                    stats["source"] += 1
                else:
                    for ext in exts:
                        ev = ext.lookup(name)
                        if ev:
                            entry["evidence"] = ev
                            stats["external-dep"] += 1
                            break
                    if not entry["evidence"]:
                        stats["unresolved"] += 1
            els_out.append(entry)
        screens_out.append({
            "id": s["id"],
            "focus": s.get("focus", ""),
            "elements": els_out,
        })

    total_named = sum(v for k, v in stats.items() if k != "unnamed")
    resolved = total_named - stats["unresolved"]
    out = {
        "package": uimap.get("package", "native:unknown"),
        "platform": uimap.get("platform", ""),
        "source_root": source,
        "stats": {
            "elements_named": total_named,
            "elements_unnamed": stats["unnamed"],
            "resolved": resolved,
            "resolve_rate": round(100 * resolved / total_named, 1)
                            if total_named else 0.0,
            "by_kind": {k: v for k, v in stats.items()},
        },
        "screens": screens_out,
    }
    with open(out_path, "w") as f:
        json.dump(out, f, indent=1)
    print(f"Bridge: {out_path}")
    print(f"  named {total_named} | resolved {resolved} "
          f"({out['stats']['resolve_rate']}%) | "
          f"source {stats['source']} / ext-dep {stats['external-dep']} / "
          f"runtime {stats['runtime-generated']} / unresolved "
          f"{stats['unresolved']}")
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser()
    ap.add_argument("uimap")
    ap.add_argument("--source", required=True,
                    help="app source tree root")
    ap.add_argument("--ext-dep", action="append", default=[],
                    help="external dependency dir (repeatable), e.g. "
                         "/usr/share/gtksourceview-4/language-specs")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    uimap = json.load(open(args.uimap))
    out = args.out or args.uimap.replace(".json", "_bridge.json")
    bridge(uimap, args.source, args.ext_dep, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
