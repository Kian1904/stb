static void
act_appmap_dialog (const char *dlg_title)
{
  GtkWidget *dlg = gtk_message_dialog_new (
      GTK_WINDOW (gtk_widget_get_toplevel (GTK_WIDGET (notebook))),
      GTK_DIALOG_MODAL, GTK_MESSAGE_INFO, GTK_BUTTONS_OK,
      "%s", dlg_title);
  gtk_window_set_title (GTK_WINDOW (dlg), dlg_title);
  gtk_dialog_run (GTK_DIALOG (dlg));
  gtk_widget_destroy (dlg);
  say ("dialog: %s", dlg_title);
}

/* Called from the dispatcher before the generic say() fallback. */
/* IDs embed the label in snake case (i_i0_4_create_folder4); normalize
 * both sides to lowercase alphanumerics before comparing. */
/* Normalize a string to lowercase alphanumerics only (caller g_free's). */
static char *
stb_norm_str (const char *s)
{
  GString *g = g_string_sized_new (64);
  const char *p;
  for (p = s; p && *p; p++)
    {
      if (g_ascii_isalnum ((guchar) *p))
        g_string_append_c (g, g_ascii_tolower (*p));
    }
  return g_string_free (g, FALSE);
}

static gboolean
stb_try_dialog_trigger (const char *id)
{
  gsize n = sizeof (stb_dialog_triggers) / sizeof (stb_dialog_triggers[0]);
  gsize i;
  if (!id)
    return FALSE;
  for (i = 0; i < n; i++)
    {
      char *nl = stb_norm_str (stb_dialog_triggers[i].label);
      char *ni = stb_norm_str (id);
      gboolean hit = (nl && ni && g_strstr_len (ni, -1, nl) != NULL);
      g_free (nl);
      g_free (ni);
      if (hit)
        {
          act_appmap_dialog (stb_dialog_triggers[i].dlg);
          return TRUE;
        }
    }
  return FALSE;
}
