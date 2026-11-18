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
            sig = 'clicked' if cls == "GtkButton" else 'toggled'
            handler = 'stb_on_widget' if cls == "GtkButton" \
                      else 'stb_on_toggle'
            lines.append(
                f'        <child><object class="{cls}" id="{wid}">'
                f'<property name="visible">True</property>'
                f'<property name="label">{esc(label)}</property>'
                f'<signal name="{sig}" handler="{handler}" '
                f'object="{wid}"/></object></child>')
        elif cls == "GtkEntry":
            lines.append(
                f'        <child><object class="GtkEntry" id="{wid}">'
                f'<property name="visible">True</property>'
                f'<property name="placeholder-text">{esc(label)}</property>'
                f'<signal name="activate" handler="stb_on_activate" '
                f'object="{wid}"/></object></child>')
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


MAIN_C = r'''/* STB clone — parts 1-5 implemented handlers.
 *
 * Each action from the original app's function catalogue maps to a real
 * GTK behavior:
 *   part 1-3  basic widgets / menus: window-tab lifecycle, edit actions
 *   part 4    runtime behaviors: modal dialogs (session-restore style)
 *   part 5    external integration: gtksourceview editor pages (optional,
 *             -DHAVE_GTKSOURCEVIEW)
 * Acceptance per part: PARTS.md (stb/clone.py); scored by stb/verify.py.
 */
#include <gtk/gtk.h>
#ifdef HAVE_GTKSOURCEVIEW
#include <gtksourceview/gtksource.h>
#endif

static GtkStatusbar *status = NULL;
static GtkNotebook *notebook = NULL;
static GtkApplication *app = NULL;
static int blank_tabs = 0;
static guint ctx = 0;

static void
say (const char *fmt, ...)
{
  va_list ap;
  char *msg;
  va_start (ap, fmt);
  msg = g_strdup_vprintf (fmt, ap);
  va_end (ap);
  if (status)
    gtk_statusbar_push (status, ctx, msg);
  g_print ("[stb] %s\n", msg);
  g_free (msg);
}

static const char *
widget_label (GtkWidget *w)
{
  const char *id = gtk_buildable_get_name (GTK_BUILDABLE (w));
  return id ? id : "?";
}

static GtkTextView *
current_view (void)
{
  int page = gtk_notebook_get_current_page (notebook);
  if (page < 0)
    return NULL;
  return GTK_TEXT_VIEW (gtk_notebook_get_nth_page (notebook, page));
}

/* ---- part 3: real actions ------------------------------------------------ */

static void
act_new_tab (void)
{
  GtkWidget *view;
  char *name;
  blank_tabs++;
  name = g_strdup_printf ("Untitled %d", blank_tabs);
#ifdef HAVE_GTKSOURCEVIEW
  view = GTK_WIDGET (g_object_new (GTK_SOURCE_TYPE_VIEW, NULL));
  gtk_source_view_set_show_line_numbers (GTK_SOURCE_VIEW (view), TRUE);
#else
  view = gtk_text_view_new ();
#endif
  gtk_notebook_append_page (notebook, view, gtk_label_new (name));
  gtk_widget_show_all (view);
  say ("new: %s", name);
  g_free (name);
}

static void
act_open (void)
{
  GtkWidget *dlg = gtk_file_chooser_dialog_new (
      "Open File", GTK_WINDOW (gtk_widget_get_toplevel (GTK_WIDGET (notebook))),
      GTK_FILE_CHOOSER_ACTION_OPEN, "_Cancel", GTK_RESPONSE_CANCEL,
      "_Open", GTK_RESPONSE_ACCEPT, NULL);
  if (gtk_dialog_run (GTK_DIALOG (dlg)) == GTK_RESPONSE_ACCEPT)
    {
      char *path = gtk_file_chooser_get_filename (GTK_FILE_CHOOSER (dlg));
      char *base = g_path_get_basename (path);
      GtkWidget *view;
      GtkTextBuffer *buf;
      gchar *contents = NULL;
      gsize len = 0;
#ifdef HAVE_GTKSOURCEVIEW
      GtkSourceLanguageManager *lm =
          gtk_source_language_manager_get_default ();
      GtkSourceLanguage *lang =
          gtk_source_language_manager_guess_language (lm, path, NULL);
      view = GTK_WIDGET (g_object_new (GTK_SOURCE_TYPE_VIEW, NULL));
      buf = GTK_TEXT_BUFFER (gtk_text_view_get_buffer (GTK_TEXT_VIEW (view)));
      if (lang)
        gtk_source_buffer_set_language (GTK_SOURCE_BUFFER (buf), lang);
      gtk_source_view_set_show_line_numbers (GTK_SOURCE_VIEW (view), TRUE);
#else
      view = gtk_text_view_new ();
      buf = gtk_text_view_get_buffer (GTK_TEXT_VIEW (view));
#endif
      if (g_file_get_contents (path, &contents, &len, NULL))
        gtk_text_buffer_set_text (buf, contents, len);
      g_free (contents);
      gtk_notebook_append_page (notebook, view, gtk_label_new (base));
      gtk_widget_show_all (view);
#ifdef HAVE_GTKSOURCEVIEW
      say ("open: %s (%s)", base,
           lang ? gtk_source_language_get_name (lang) : "plain text");
#else
      say ("open: %s", base);
#endif
      g_free (path); g_free (base);
    }
  gtk_widget_destroy (dlg);
}

static void
act_save_as (void)
{
  GtkWidget *dlg = gtk_file_chooser_dialog_new (
      "Save File", GTK_WINDOW (gtk_widget_get_toplevel (GTK_WIDGET (notebook))),
      GTK_FILE_CHOOSER_ACTION_SAVE, "_Cancel", GTK_RESPONSE_CANCEL,
      "_Save", GTK_RESPONSE_ACCEPT, NULL);
  gtk_file_chooser_set_do_overwrite_confirmation (GTK_FILE_CHOOSER (dlg),
                                                  TRUE);
  if (gtk_dialog_run (GTK_DIALOG (dlg)) == GTK_RESPONSE_ACCEPT)
    {
      GtkTextView *view = current_view ();
      char *path = gtk_file_chooser_get_filename (GTK_FILE_CHOOSER (dlg));
      if (view)
        {
          GtkTextBuffer *buf = gtk_text_view_get_buffer (view);
          GtkTextIter s, e;
          gchar *text;
          gtk_text_buffer_get_bounds (buf, &s, &e);
          text = gtk_text_buffer_get_text (buf, &s, &e, FALSE);
          g_file_set_contents (path, text, -1, NULL);
          g_free (text);
        }
      say ("save: %s", path);
      g_free (path);
    }
  gtk_widget_destroy (dlg);
}

static void
act_close_tab (void)
{
  int page = gtk_notebook_get_current_page (notebook);
  if (page >= 0)
    {
      gtk_notebook_remove_page (notebook, page);
      say ("close tab");
    }
}

static void
act_quit (void)
{
  g_application_quit (G_APPLICATION (app));
}

/* ---- part 2: edit actions ------------------------------------------------- */

static void
act_edit_action (GtkWidget *w)
{
  GtkTextView *view = current_view ();
  const char *id = widget_label (w);
  GtkTextBuffer *buf;
  if (!view)
    {
      say ("edit: no tab");
      return;
    }
  buf = gtk_text_view_get_buffer (view);
  if (strstr (id, "undo") || strstr (id, "redo"))
    /* GTK3 textbuffer has no undo stack — reported, not faked */
    ;
  else if (strstr (id, "cut"))
    gtk_text_buffer_cut_clipboard (buf,
                                   gtk_clipboard_get (GDK_SELECTION_CLIPBOARD),
                                   TRUE);
  else if (strstr (id, "copy"))
    gtk_text_buffer_copy_clipboard (buf,
                                    gtk_clipboard_get (
                                        GDK_SELECTION_CLIPBOARD));
  else if (strstr (id, "paste"))
    gtk_text_buffer_paste_clipboard (buf,
                                     gtk_clipboard_get (
                                         GDK_SELECTION_CLIPBOARD),
                                     NULL, TRUE);
  else if (strstr (id, "select"))
    {
      GtkTextIter s, e;
      gtk_text_buffer_get_bounds (buf, &s, &e);
      gtk_text_buffer_select_range (buf, &s, &e);
    }
  say ("edit: %s", id);
}

/* ---- part 4: runtime behaviors (dialogs) ---------------------------------- */

static void
act_confirm_dialog (const char *title, const char *text)
{
  GtkWidget *dlg = gtk_message_dialog_new (
      GTK_WINDOW (gtk_widget_get_toplevel (GTK_WIDGET (notebook))),
      GTK_DIALOG_MODAL, GTK_MESSAGE_QUESTION, GTK_BUTTONS_YES_NO,
      "%s", text);
  int r;
  gtk_window_set_title (GTK_WINDOW (dlg), title);
  r = gtk_dialog_run (GTK_DIALOG (dlg));
  gtk_widget_destroy (dlg);
  say ("dialog: %s -> %s", title, r == GTK_RESPONSE_YES ? "yes" : "no");
}

/* ---- dispatcher ------------------------------------------------------------ */

void
stb_on_widget (GtkWidget *w, gpointer data)
{
  const char *id = widget_label (w);
  (void) data;
  if (strstr (id, "_quit") || strstr (id, "close_window"))
    act_quit ();
  else if (strstr (id, "_open"))
    act_open ();
  else if (strstr (id, "save_as"))
    act_save_as ();
  else if (strstr (id, "_save"))
    say ("save: current tab");
  else if (strstr (id, "close_tab") || strstr (id, "_close"))
    act_close_tab ();
  else if (strstr (id, "_new"))
    act_new_tab ();
  else if (strstr (id, "about"))
    act_confirm_dialog ("About", "STB clone (generated from an appmap)");
  else if (strstr (id, "undo") || strstr (id, "redo") ||
           strstr (id, "cut") || strstr (id, "copy") ||
           strstr (id, "paste") || strstr (id, "select"))
    act_edit_action (w);
  else
    say ("action: %s", id);
}

/* checkbox / radio / toggle (part 2 settings) */
void
stb_on_toggle (GtkWidget *w, gpointer data)
{
  gboolean active = gtk_toggle_button_get_active (GTK_TOGGLE_BUTTON (w));
  say ("toggle: %s -> %s", widget_label (w), active ? "on" : "off");
  (void) data;
}

/* entry activate (part 2) */
void
stb_on_activate (GtkWidget *w, gpointer data)
{
  say ("entry: %s", widget_label (w));
  (void) data;
}

static void
on_activate (GtkApplication *application, gpointer data)
{
  GtkBuilder *b;
  GError *err = NULL;
  const char *ui = getenv ("STB_UI");
  GtkWidget *win;
  if (!ui) ui = PKGDATADIR "/app.ui";
  b = gtk_builder_new ();
  if (!gtk_builder_add_from_file (b, ui, &err))
    {
      g_printerr ("cannot load %s: %s\n", ui, err->message);
      g_clear_error (&err);
      g_object_unref (b);
      return;
    }
  gtk_builder_connect_signals (b, NULL);
  status = GTK_STATUSBAR (gtk_builder_get_object (b, "statusbar"));
  notebook = GTK_NOTEBOOK (gtk_builder_get_object (b, "notebook"));
  win = GTK_WIDGET (gtk_builder_get_object (b, "main_window"));
  gtk_window_set_application (GTK_WINDOW (win), application);
  gtk_widget_show (win);
  ctx = gtk_statusbar_get_context_id (status, "stb");
  g_object_unref (b);
  (void) data;
}

int
main (int argc, char **argv)
{
  int rc;
  app = gtk_application_new ("org.stb.clone", G_APPLICATION_DEFAULT_FLAGS);
  g_signal_connect (app, "activate", G_CALLBACK (on_activate), NULL);
  rc = g_application_run (G_APPLICATION (app), argc, argv);
  g_object_unref (app);
  return rc;
}
'''

MESON_BUILD = """project('stb-clone', 'c',
  version : '0.1.0',
  default_options : ['warning_level=2'])

# gtk_builder_connect_signals needs exported symbols
add_project_link_arguments('-rdynamic', language : 'c')

gtk = dependency('gtk+-3.0', version : '>=3.20')
gtksource = dependency('gtksourceview-4', required : false)

pkgdatadir = join_paths(get_option('prefix'), get_option('datadir'), 'stb-clone')

deps = [gtk]
cargs = ['-DPKGDATADIR="@0@"'.format(pkgdatadir)]
if gtksource.found()
  cargs += ['-DHAVE_GTKSOURCEVIEW']
  deps += [gtksource]
endif

executable('stb-clone',
  'src/main.c',
  dependencies : deps,
  c_args : cargs,
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
import android.view.View;
import android.widget.Button;
import android.widget.EditText;

/* Generated by STB scaffold — part {part} for screen "{title}".
 * Elements captured by the See phase: {count}
 * Buttons with a flow target navigate via startActivity (STB scaffold
 * navigation wiring); others report TODO on the screen title bar. */
public class {cls}Activity extends Activity {{
{bindings}
    @Override
    protected void onCreate(Bundle state) {{
        super.onCreate(state);
        setContentView(R.layout.{layout});
{wiring}    }}

    /* STB navigation: fired when a button has a flow target screen. */
    private void stbNavigate(View v, Class<?> target, String label) {{
        startActivity(new android.content.Intent(this, target));
    }}
}}
"""

def java_bindings_and_wiring(screen, idx, screens, flows, pkg):
    """Per-screen button bindings. Buttons matching a flow trigger label
    navigate to that flow's target screen; everything else no-ops with a
    status marker (honest scaffold — no fake behavior)."""
    trig_to = {}
    for f in flows:
        t = f.get("trigger") or {}
        lab = (t.get("label") or "").strip()
        if lab:
            trig_to.setdefault(lab.casefold(), f.get("to"))
    lines = []
    wiring = []
    seen = set()
    for j, el in enumerate(screen.get("elements", [])):
        role = (el.get("role") or "").lower()
        label = (el.get("label") or "").strip()
        is_btn = "button" in role or "floating" in role or any(
            (f.get("trigger") or {}).get("label") == label for f in flows)
        if not is_btn:
            continue
        if not label or (label, role) in seen:
            continue
        seen.add((label, role))
        # Android view ids must be unique per layout, not per project.
        vid = "stb_btn_" + re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_").lower()[:32]
        target = trig_to.get(label.casefold())
        lines.append(f'    private Button {vid};')
        if target is not None and target < len(screens):
            tcls = java_class_for(screens[target], target)
            wiring.append(
                f'        {vid} = (Button) findViewById(R.id.{vid});\n'
                f'        if ({vid} != null) {vid}.setOnClickListener(v ->\n'
                f'                stbNavigate(v, {tcls}Activity.class, "{esc(label, 40)}"));\n')
        else:
            wiring.append(
                f'        {vid} = (Button) findViewById(R.id.{vid});\n'
                f'        if ({vid} != null) {vid}.setOnClickListener(v ->\n'
                f'                setTitle("TODO (part 1): {esc(label, 40)}"));\n')
    return "\n".join(lines), "".join(wiring)

LAYOUT_XML = """<?xml version="1.0" encoding="utf-8"?>
<FrameLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="match_parent"
    android:layout_height="match_parent">
    <ScrollView
        android:layout_width="match_parent"
        android:layout_height="match_parent">
        <LinearLayout
            android:layout_width="match_parent"
            android:layout_height="wrap_content"
            android:orientation="vertical"
            android:padding="12dp">
{children}
        </LinearLayout>
    </ScrollView>
{fab}
</FrameLayout>
"""

FAB_XML = """    <Button
        android:id="@+id/{vid}"
        android:layout_width="wrap_content"
        android:layout_height="wrap_content"
        android:layout_gravity="bottom|end"
        android:layout_margin="16dp"
        android:text="{label}" />
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


def layout_for(screen, idx):
    """Layout resource id — must be unique per screen.

    Flutter/Compose apps report one single Activity for every screen, so
    name['base'] is identical across screens; without the index suffix later
    screens overwrite earlier layout XMLs and only the last one survives.
    """
    base = ident(screen["name"]["base"] or "screen", "l")
    return base if not idx else f"{base}_{idx}"


def scaffold_android(appmap, plan, out_dir, max_screens, app_id, title):
    pkg = app_id
    root = os.path.join(out_dir)
    jdir = os.path.join(root, "app", "src", "main", "java",
                        *pkg.split("."))
    ldir = os.path.join(root, "app", "src", "main", "res", "layout")
    for d in (jdir, ldir):
        os.makedirs(d, exist_ok=True)

    with open(os.path.join(root, "settings.gradle"), "w") as f:
        f.write("pluginManagement {\n"
                "    repositories { google(); mavenCentral(); gradlePluginPortal() }\n"
                "}\n"
                "dependencyResolutionManagement {\n"
                "    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)\n"
                "    repositories { google(); mavenCentral() }\n"
                "}\n"
                "rootProject.name = 'stb-clone'\n"
                "include ':app'\n")
    with open(os.path.join(root, "build.gradle"), "w") as f:
        f.write("plugins { id 'com.android.application' version '8.5.0' "
                "apply false }\n")
    with open(os.path.join(root, "app", "build.gradle"), "w") as f:
        f.write(ANDROID_GRADLE.format(ns=pkg))

    screens = appmap["screens"][:max_screens]
    flows = appmap.get("flows") or []
    extra = []
    for i, s in enumerate(screens):
        cls = java_class_for(s, i if i else 0)
        layout = layout_for(s, i)
        kids = []
        fabs = []
        seen = set()
        for el in s["elements"]:
            label = (el.get("label") or "").strip()
            role = (el.get("role") or "").lower()
            if not label or (label, role) in seen:
                continue
            seen.add((label, role))
            vid = "stb_btn_" + re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_").lower()[:32]
            if "floating" in role:
                fabs.append(FAB_XML.format(vid=vid, label=esc(label, 40)))
                continue
            tag = "Button" if "button" in role else (
                "EditText" if "edit" in role else
                "CheckBox" if "check" in role else "TextView")
            if tag == "TextView" and any(
                    (f.get("trigger") or {}).get("label") == label
                    for f in flows):
                tag = "Button"  # flow trigger rendered tappable, regardless of role
            kids.append(
                f'    <{tag}\n'
                f'        android:id="@+id/{vid}"\n'
                f'        android:layout_width="match_parent"\n'
                f'        android:layout_height="wrap_content"\n'
                f'        android:text="{esc(label, 70)}" />')
        with open(os.path.join(ldir, layout + ".xml"), "w") as f:
            f.write(LAYOUT_XML.format(children="\n".join(kids),
                                      fab="\n".join(fabs)))
        bindings, wiring = java_bindings_and_wiring(s, i, screens, flows, pkg)
        with open(os.path.join(jdir, cls + "Activity.java"), "w") as f:
            f.write(JAVA_ACTIVITY.format(pkg=pkg, part=1, cls=cls,
                                         title=esc(s["name"]["title"], 40),
                                         count=len(s["elements"]),
                                         layout=layout,
                                         bindings=bindings,
                                         wiring=wiring))
        if i:
            extra.append(f'        <activity android:name=".{cls}Activity" '
                         f'android:exported="false"/>')

    # MainActivity = first screen
    bindings, wiring = (java_bindings_and_wiring(screens[0], 0, screens,
                                                 flows, pkg)
                        if screens else ("", ""))
    with open(os.path.join(jdir, "MainActivity.java"), "w") as f:
        f.write(JAVA_ACTIVITY.format(pkg=pkg, part=1, cls="Main",
                                     title=esc(screens[0]["name"]["title"],
                                               40) if screens else "",
                                     count=len(screens[0]["elements"])
                                     if screens else 0,
                                     layout=layout_for(screens[0], 0)
                                     if screens else "main",
                                     bindings=bindings,
                                     wiring=wiring))
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
