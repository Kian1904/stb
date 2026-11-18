#!/usr/bin/env python3
"""STB Scaffold — turn an AppMap + clone plan into a compilable project.

Native/GTK target (verified, builds & runs):
  proj/
    meson.build
    src/main.c
    data/app.ui          GtkBuilder XML generated from the appmap
    README.md, PARTS.md  part-by-part build order + acceptance criteria
    parts.json           machine-readable part/stub index

Android target (files only — requires Android SDK to build):
  proj/
    settings.gradle, build.gradle, app/build.gradle
    app/src/main/AndroidManifest.xml
    app/src/main/java/<pkg>/<Screen>Activity.java
    app/src/main/res/layout/<screen>.xml

Every generated widget is tagged with the appmap function it stands for, so
a later See(clone) pass can diff labels against the original appmap.

Nothing here is trusted instructions: the app's labels are DATA copied into
a template (docs/UNTRUSTED_CONTENT.md). Labels are XML-escaped and capped.

Usage:
  python3 stb/scaffold.py <appmap.json> [<clone_plan.json>]
      [--target native-gtk|android] [--out-dir DIR] [--app-id ID]
      [--title NAME] [--max-screens N]
"""
import argparse
import json
import os
import re
import sys

GTK_ROLE_MAP = [
    (("menu bar",), "GtkMenuBar"),
    (("menu item", "menuitem"), "GtkMenuItem"),
    (("menu",), "GtkMenuItem"),
    (("check box", "checkbox"), "GtkCheckButton"),
    (("radio",), "GtkRadioButton"),
    (("combo", "spinner"), "GtkComboBoxText"),
    (("entry", "edit"), "GtkEntry"),
    (("tab",), "GtkLabel"),
    (("push button", "button"), "GtkButton"),
    (("status bar",), "GtkStatusbar"),
    (("label", "text"), "GtkLabel"),
]


def gtk_class(role):
    r = (role or "").lower()
    for keys, cls in GTK_ROLE_MAP:
        if any(k in r for k in keys):
            return cls
    return "GtkLabel"


def esc(s, limit=80):
    s = (s or "").strip().replace("\n", " ")[:limit]
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def ident(s, prefix="id"):
    s = re.sub(r"[^A-Za-z0-9]+", "_", (s or "")).strip("_").lower()
    return f"{prefix}_{s or 'x'}"[:48]


# --------------------------------------------------------------------------
# GTK scaffolding
# --------------------------------------------------------------------------

def collect_menubar(screens):
    """Rebuild the menu hierarchy from the flattened AT-SPI dump.

    The dump is depth-first: a top-level 'menu' opens a group; a 'menu'
    inside a group is a submenu of the previous item/group; 'menu item'
    belongs to the innermost open group. Returns nested tree
    [{label, items:[{label} | {label, items:[...]}]}].
    """
    best = []
    for s in screens:
        # root = top-level menus in order
        root = []
        stack = []  # list of open groups: dicts with "items"
        for el in s["elements"]:
            role = (el.get("role") or "").lower()
            label = (el.get("label") or "").strip()
            if not label or "menu bar" in role:
                continue
            if role == "menu":
                if not stack:
                    g = {"label": label, "items": []}
                    root.append(g)
                    stack.append(g)
                else:
                    # submenu of the previous item: wrap it
                    parent = stack[-1]
                    if parent["items"] and isinstance(parent["items"][-1],
                                                      dict):
                        sub = parent["items"][-1]
                    else:
                        sub = {"label": label, "items": []}
                        parent["items"].append(sub)
                    g = {"label": label, "items": []}
                    # previous bare item becomes the submenu header
                    if parent["items"] and isinstance(parent["items"][-1],
                                                      str):
                        g["label"] = parent["items"].pop()
                    parent["items"].append(g)
                    stack.append(g)
            elif "menu item" in role:
                if stack:
                    stack[-1]["items"].append(label)
                # else: stray item (dialog menus) — skip
            else:
                # any non-menu role closes open groups (end of menubar)
                if stack:
                    stack = []
        total = sum(len(m["items"]) for m in root)
        if total > sum(len(m["items"]) for m in best):
            best = root
    return best


def emit_menu(group, depth, lines, counter):
    """Emit GtkMenuItem (+submenu if it has items) for one group."""
    counter[0] += 1
    mid = uniq(ident(f"d{depth}_{counter[0]}_{group['label']}", "m"))
    lines.append(f'<child><object class="GtkMenuItem" id="{mid}">'
                 f'<property name="visible">True</property>'
                 f'<property name="label">{esc(group["label"])}</property>')
    if group["items"]:
        lines.append('<child type="submenu"><object class="GtkMenu">'
                     '<property name="visible">True</property>')
        for it in group["items"]:
            if isinstance(it, dict):
                emit_menu(it, depth + 1, lines, counter)
            else:
                counter[0] += 1
                iid = uniq(ident(f"i{depth}_{counter[0]}_{it}", "i"))
                lines.append(f'<child><object class="GtkMenuItem" id="{iid}">'
                             f'<property name="visible">True</property>'
                             f'<property name="label">{esc(it)}</property>'
                             f'<signal name="activate" '
                             f'handler="stb_on_widget" object="{iid}"/>'
                             f'</object></child>')
        lines.append('</object></child>')
    lines.append('</object></child>')


_UID = {"n": 0}


def uniq(prefix):
    _UID["n"] += 1
    return f"{prefix}{_UID['n']}"


def screen_page_ui(s, panel_prefix):
    """A GtkBox page holding the screen's non-menu widgets."""
    lines = []
    grid_id = ident(f"grid_{s['id']}", "g")
    lines.append(f'        <child><object class="GtkScrolledWindow">'
                 f'<property name="visible">True</property>'
                 f'<child><object class="GtkViewport">'
                 f'<property name="visible">True</property>'
                 f'<child><object class="GtkBox"><property name="visible">'
                 f'True</property><property name="orientation">vertical'
                 f'</property><property name="spacing">6</property>'
                 f'<property name="margin">12</property>')
    seen = set()
    for el in s["elements"]:
        role = (el.get("role") or "").lower()
        label = (el.get("label") or "").strip()
        if not label or "menu" in role or "page tab" in role:
            continue
        key = (label, role)
        if key in seen:
            continue
        seen.add(key)
        cls = gtk_class(role)
        wid = uniq(ident(f"{s['id']}_{label}", "w"))
        if cls == "GtkStatusbar":
            continue
        if cls in ("GtkButton", "GtkCheckButton", "GtkRadioButton"):
            lines.append(
                f'        <child><object class="{cls}" id="{wid}">'
                f'<property name="visible">True</property>'
                f'<property name="label">{esc(label)}</property>'
                f'<signal name="clicked" handler="stb_on_widget" '
                f'object="{wid}"/></object></child>')
        elif cls == "GtkEntry":
            lines.append(
                f'        <child><object class="GtkEntry" id="{wid}">'
                f'<property name="visible">True</property>'
                f'<property name="placeholder-text">{esc(label)}</property>'
                f'</object></child>')
        elif cls == "GtkComboBoxText":
            lines.append(
                f'        <child><object class="GtkComboBoxText" id="{wid}">'
                f'<property name="visible">True</property>'
                f'<property name="tooltip-text">{esc(label)}</property>'
                f'</object></child>')
        else:
            lines.append(
                f'        <child><object class="GtkLabel" id="{wid}">'
                f'<property name="visible">True</property>'
                f'<property name="xalign">0</property>'
                f'<property name="label">{esc(label)}</property>'
                f'</object></child>')
    lines.append('        </object></child></object></child></object>'
                 '</child>')
    return "\n".join(lines), len(seen)


def gen_gtk_ui(appmap, max_screens):
    screens = appmap["screens"][:max_screens]
    menus = collect_menubar(appmap["screens"])

    ui = ['<?xml version="1.0" encoding="UTF-8"?>', '<interface>']
    # ---- main window
    ui.append('<object class="GtkApplicationWindow" id="main_window">'
              '<property name="can_focus">False</property>'
              '<property name="title">STB clone</property>'
              '<property name="default_width">900</property>'
              '<property name="default_height">600</property>'
              '<child><object class="GtkBox">'
              '<property name="visible">True</property>'
              '<property name="orientation">vertical</property>')

    # ---- menubar
    if menus:
        ui.append('<child><object class="GtkMenuBar" id="menubar">'
                  '<property name="visible">True</property>')
        cnt = [0]
        for g in menus:
            emit_menu(g, 0, ui, cnt)
        ui.append('</object></child>')

    # ---- notebook: one page per observed screen
    ui.append('<child><object class="GtkNotebook" id="notebook">'
              '<property name="visible">True</property>'
              '<property name="hexpand">True</property>'
              '<property name="vexpand">True</property>')
    for s in screens:
        title = s["name"]["title"] or s["name"]["base"]
        page, n = screen_page_ui(s, "p")
        ui.append(f'<child><object class="GtkBox"><property name="visible">'
                  f'True</property><property name="orientation">vertical'
                  f'</property>{page}</object></child>')
    ui.append('</object></child>')

    # ---- statusbar
    ui.append('<child><object class="GtkStatusbar" id="statusbar">'
              '<property name="visible">True</property></object></child>')
    ui.append('</object></child></object>')
    ui.append('</interface>')
    return "\n".join(ui) + "\n"


MAIN_C = r'''/* Generated by STB scaffold (stb/scaffold.py). Part skeletons live in
 * PARTS.md; each handler below is a stub marked with the part that must
 * fill it in. */
#include <gtk/gtk.h>

static GtkStatusbar *status = NULL;

void
stb_on_widget (GtkWidget *w, gpointer data)
{
  const char *id = gtk_buildable_get_name (GTK_BUILDABLE (w));
  char *msg = g_strdup_printf ("TODO (part %s): %s", "1", id ? id : "?");
  if (status)
    gtk_statusbar_push (status, 0, msg);
  g_print ("[stb] %s\n", msg);
  g_free (msg);
}

static void
on_activate (GtkApplication *app, gpointer data)
{
  GtkBuilder *b = gtk_builder_new ();
  GError *err = NULL;
  const char *ui = getenv ("STB_UI");
  if (!ui) ui = PKGDATADIR "/app.ui";
  if (!gtk_builder_add_from_file (b, ui, &err)) {
    g_printerr ("cannot load %s: %s\n", ui, err->message);
    g_clear_error (&err);
    g_object_unref (b);
    return;
  }
  gtk_builder_connect_signals (b, NULL);
  status = GTK_STATUSBAR (gtk_builder_get_object (b, "statusbar"));
  GtkWidget *win = GTK_WIDGET (gtk_builder_get_object (b, "main_window"));
  gtk_window_set_application (GTK_WINDOW (win), app);
  gtk_widget_show (win);
  g_object_unref (b);
}

int
main (int argc, char **argv)
{
  GtkApplication *app;
  int status_rc;
  app = gtk_application_new ("org.stb.clone", G_APPLICATION_DEFAULT_FLAGS);
  g_signal_connect (app, "activate", G_CALLBACK (on_activate), NULL);
  status_rc = g_application_run (G_APPLICATION (app), argc, argv);
  g_object_unref (app);
  return status_rc;
}
'''

MESON_BUILD = """project('stb-clone', 'c',
  version : '0.1.0',
  default_options : ['warning_level=2'])

# gtk_builder_connect_signals needs exported symbols
add_project_link_arguments('-rdynamic', language : 'c')

gtk = dependency('gtk+-3.0', version : '>=3.20')

pkgdatadir = join_paths(get_option('prefix'), get_option('datadir'), 'stb-clone')

executable('stb-clone',
  'src/main.c',
  dependencies : [gtk],
  c_args : ['-DPKGDATADIR="@0@"'.format(pkgdatadir)],
  install : true)

install_data('data/app.ui', install_dir : pkgdatadir)
"""


def scaffold_gtk(appmap, plan, out_dir, max_screens):
    os.makedirs(os.path.join(out_dir, "src"), exist_ok=True)
    os.makedirs(os.path.join(out_dir, "data"), exist_ok=True)
    ui = gen_gtk_ui(appmap, max_screens)
    with open(os.path.join(out_dir, "data", "app.ui"), "w") as f:
        f.write(ui)
    with open(os.path.join(out_dir, "src", "main.c"), "w") as f:
        f.write(MAIN_C)
    with open(os.path.join(out_dir, "meson.build"), "w") as f:
        f.write(MESON_BUILD)

    parts = plan.get("build_parts", []) if plan else []
    write_parts_docs(out_dir, appmap, parts, "native-gtk")
    return {"ui_bytes": len(ui), "parts": len(parts)}


JAVA_ACTIVITY = """package {pkg};

import android.os.Bundle;
import android.app.Activity;
import android.widget.TextView;
import android.widget.LinearLayout;

/* Generated by STB scaffold — part {part} placeholder for screen "{title}".
 * Elements captured by the See phase: {count} */
public class {cls}Activity extends Activity {{
    @Override
    protected void onCreate(Bundle state) {{
        super.onCreate(state);
        setContentView(R.layout.{layout});
    }}
}}
"""

LAYOUT_XML = """<?xml version="1.0" encoding="utf-8"?>
<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="match_parent"
    android:layout_height="match_parent"
    android:orientation="vertical"
    android:padding="12dp">
{children}
</LinearLayout>
"""

ANDROID_GRADLE = """plugins {{ id 'com.android.application' }}
android {{
    namespace '{ns}'
    compileSdk 34
    defaultConfig {{
        applicationId '{ns}'
        minSdk 24
        targetSdk 34
        versionCode 1
        versionName "0.1.0-stb-scaffold"
    }}
    buildTypes {{ release {{ minifyEnabled false }} }}
    compileOptions {{
        sourceCompatibility JavaVersion.VERSION_17
        targetCompatibility JavaVersion.VERSION_17
    }}
}}
"""

MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">
    <application android:label="{title}">
        <activity android:name=".MainActivity"
                  android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.MAIN"/>
                <category android:name="android.intent.category.LAUNCHER"/>
            </intent-filter>
        </activity>
{extra}
    </application>
</manifest>
"""


def java_class_for(screen, idx):
    base = re.sub(r"[^A-Za-z0-9]", "", screen["name"]["base"] or "Screen")
    if not base or base[0].isdigit():
        base = "S" + base
    return f"{base}{idx}"


def scaffold_android(appmap, plan, out_dir, max_screens, app_id, title):
    pkg = app_id
    root = os.path.join(out_dir)
    jdir = os.path.join(root, "app", "src", "main", "java",
                        *pkg.split("."))
    ldir = os.path.join(root, "app", "src", "main", "res", "layout")
    for d in (jdir, ldir):
        os.makedirs(d, exist_ok=True)

    with open(os.path.join(root, "settings.gradle"), "w") as f:
        f.write("rootProject.name = 'stb-clone'\n"
                "include ':app'\n")
    with open(os.path.join(root, "build.gradle"), "w") as f:
        f.write("plugins { id 'com.android.application' version '8.5.0' "
                "apply false }\n")
    with open(os.path.join(root, "app", "build.gradle"), "w") as f:
        f.write(ANDROID_GRADLE.format(ns=pkg))

    screens = appmap["screens"][:max_screens]
    extra = []
    for i, s in enumerate(screens):
        cls = java_class_for(s, i if i else 0)
        layout = ident(s["name"]["base"] or f"screen_{i}", "l")
        kids = []
        seen = set()
        for el in s["elements"]:
            label = (el.get("label") or "").strip()
            role = (el.get("role") or "").lower()
            if not label or (label, role) in seen:
                continue
            seen.add((label, role))
            tag = "Button" if "button" in role else (
                "EditText" if "edit" in role else
                "CheckBox" if "check" in role else "TextView")
            kids.append(
                f'    <{tag}\n'
                f'        android:layout_width="match_parent"\n'
                f'        android:layout_height="wrap_content"\n'
                f'        android:text="{esc(label, 70)}" />')
        with open(os.path.join(ldir, layout + ".xml"), "w") as f:
            f.write(LAYOUT_XML.format(children="\n".join(kids)))
        with open(os.path.join(jdir, cls + "Activity.java"), "w") as f:
            f.write(JAVA_ACTIVITY.format(pkg=pkg, part=1, cls=cls,
                                         title=esc(s["name"]["title"], 40),
                                         count=len(s["elements"]),
                                         layout=layout))
        if i:
            extra.append(f'        <activity android:name=".{cls}Activity" '
                         f'android:exported="false"/>')

    # MainActivity = first screen
    with open(os.path.join(jdir, "MainActivity.java"), "w") as f:
        f.write(JAVA_ACTIVITY.format(pkg=pkg, part=1, cls="Main",
                                     title=esc(screens[0]["name"]["title"],
                                               40) if screens else "",
                                     count=len(screens[0]["elements"])
                                     if screens else 0,
                                     layout=ident(screens[0]["name"]["base"],
                                                  "l") if screens else "main"))
    with open(os.path.join(root, "app", "src", "main",
                           "AndroidManifest.xml"), "w") as f:
        f.write(MANIFEST.format(title=esc(title or "stb clone"),
                                extra="\n".join(extra)))

    parts = plan.get("build_parts", []) if plan else []
    write_parts_docs(out_dir, appmap, parts, "android")
    return {"activities": len(screens) + 1, "parts": len(parts)}


def write_parts_docs(out_dir, appmap, parts, target):
    with open(os.path.join(out_dir, "parts.json"), "w") as f:
        json.dump({"target": target, "parts": parts}, f, indent=1)
    lines = [f"# Build plan — {appmap.get('meta', {}).get('package')}",
             "", f"Target: **{target}** · parts sorted easy -> hard.", ""]
    for p in parts:
        lines += [f"## Part {p['id']} — {p['title']} "
                  f"(difficulty {p['difficulty']})",
                  "", p.get("description", ""), "",
                  f"- screens: {p.get('screens')}",
                  f"- functions: {p.get('function_count')}",
                  f"- depends on: {p.get('depends_on') or '—'}",
                  f"- tools: {', '.join(p.get('tools_needed', []))}",
                  "- acceptance (labels the clone must show): "
                  f"{', '.join(p.get('acceptance', {}).get('must_see_elements', [])[:10])}",
                  ""]
    with open(os.path.join(out_dir, "PARTS.md"), "w") as f:
        f.write("\n".join(lines))
    with open(os.path.join(out_dir, "README.md"), "w") as f:
        f.write(f"# STB clone scaffold\n\nGenerated from an STB AppMap of "
                f"`{appmap.get('meta', {}).get('package')}`.\n\n"
                f"- target: {target}\n- parts: {len(parts)} "
                f"(see PARTS.md)\n\n"
                "All labels are data captured from the original app; "
                "build logic is stubbed per part.\n")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser()
    ap.add_argument("appmap")
    ap.add_argument("plan", nargs="?", default=None)
    ap.add_argument("--target", default=None,
                    choices=["native-gtk", "android"])
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--app-id", default=None)
    ap.add_argument("--title", default=None)
    ap.add_argument("--max-screens", type=int, default=6)
    args = ap.parse_args(argv)

    appmap = json.load(open(args.appmap))
    plan = {}
    plan_path = args.plan
    if not plan_path:
        cand = args.appmap.replace("_appmap.json", "_clone_plan.json")
        if os.path.exists(cand):
            plan_path = cand
    if plan_path and os.path.exists(plan_path):
        plan = json.load(open(plan_path))

    platform = appmap.get("meta", {}).get("platform", "android")
    target = args.target or ("native-gtk" if ("atspi" in platform
                                              or "x11" in platform)
                             else "android")
    pkg = appmap.get("meta", {}).get("package") or "app"
    app_id = args.app_id or "org.stb.clone"
    if target == "android":
        app_id = args.app_id or ("org.stb." + re.sub(
            r"[^a-z0-9]", "", pkg.lower())[:20])

    out_dir = args.out_dir or os.path.join(
        "/tmp", "stb_clone_" + re.sub(r"[^A-Za-z0-9]", "_", pkg)[:24])
    os.makedirs(out_dir, exist_ok=True)

    if target == "native-gtk":
        info = scaffold_gtk(appmap, plan, out_dir, args.max_screens)
    else:
        info = scaffold_android(appmap, plan, out_dir, args.max_screens,
                                app_id, args.title or pkg)

    print(f"Scaffold: {out_dir} (target={target})")
    for k, v in info.items():
        print(f"  {k}: {v}")
    print(f"  files: {sum(len(f) for _, _, f in os.walk(out_dir))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
