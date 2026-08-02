"""The visual language of the browser, in one file.

Chrome stays minimal -- one 40px bar -- but not colourless. The palette is
Claude's: a warm coral-orange accent against warm neutrals, rather than the
blue-grey default every GTK app already looks like.

Colour is used for meaning, never decoration:
  accent   the active mode, focus, progress, the prompt caret
  ok       a finished run
  warn     something failed, and why
Everything else is neutral so those three read instantly.
"""

# Claude's warm neutrals and coral accent. The dark surfaces are warm-tinted
# (note the red channel leads) so the orange sits on them without clashing.
_DARK = {
    "bg": "#1f1e1d",
    "bar": "#262624",
    "panel": "#232221",
    "line": "#3a3937",
    "text": "#f0eee6",
    "dim": "#9a968c",
    "field": "#2a2927",
    "field_focus": "#302e2c",
    "accent": "#d97757",       # Claude coral
    "accent_soft": "#4a2e24",  # accent at low alpha, pre-blended for GTK3
    "on_accent": "#1f1e1d",
    "ok": "#7fb069",
    "warn": "#e0894f",
    "tab_active": "#2f2d2b",
}

# On white, the coral has to darken to stay readable as text (#d97757 is only
# ~2.9:1 on white; this is ~4.6:1). Same hue, different job.
_LIGHT = {
    "bg": "#ffffff",
    "bar": "#faf9f5",
    "panel": "#f7f5ef",
    "line": "#e5e1d8",
    "text": "#3d3d3a",
    "dim": "#6f6c63",
    "field": "#ffffff",
    "field_focus": "#ffffff",
    "accent": "#b8532f",
    "accent_soft": "#f6e4dc",
    "on_accent": "#ffffff",
    "ok": "#4a7c3f",
    "warn": "#a8541f",
    "tab_active": "#ffffff",
}

_TEMPLATE = """
window, .cb-root {{ background: {bg}; }}

/* ---- the one bar ---- */
.cb-bar {{
    background: {bar};
    border-bottom: 1px solid {line};
    padding: 4px 6px;
}}

.cb-nav button {{
    background: transparent;
    border: none;
    box-shadow: none;
    color: {dim};
    padding: 4px 7px;
    margin: 0 1px;
    min-width: 20px;
    min-height: 20px;
    border-radius: 6px;
}}
.cb-nav button:hover {{ color: {text}; background: {field_focus}; }}
.cb-nav button:active {{ color: {accent}; }}
.cb-nav button:disabled {{ color: {line}; }}

/* The Claude actions get the accent, so they read as a group. */
.cb-nav button.cb-ai {{ color: {accent}; }}
.cb-nav button.cb-ai:hover {{ background: {accent_soft}; color: {accent}; }}

/* The bookmark star: lit means saved. The one control here whose colour is
   state rather than decoration. */
.cb-nav button.cb-star.on {{ color: {accent}; }}
.cb-nav button.cb-star.on:hover {{ background: {accent_soft}; }}

/* ---- private tabs ----
   A private window must be obvious at a glance without being loud: a dashed
   accent underline on the bar, and a badge on the tab itself. */
.cb-private .cb-bar {{
    border-bottom: 1px dashed {accent};
    background: {panel};
}}
.cb-priv-badge {{
    background: {accent_soft};
    color: {accent};
    font-size: 0.68em;
    font-weight: 700;
    padding: 1px 5px;
    border-radius: 4px;
}}

/* ---- omnibox ---- */
.cb-omnibox {{
    background: {field};
    color: {text};
    border: 1px solid {line};
    border-radius: 8px;
    padding: 5px 11px;
    margin: 0 6px;
    caret-color: {accent};
}}
.cb-omnibox:focus {{
    background: {field_focus};
    border-color: {accent};
    box-shadow: none;
}}
.cb-omnibox selection {{ background: {accent}; color: {on_accent}; }}

/* ---- load progress: a 2px accent hairline ---- */
.cb-progress {{ background: transparent; min-height: 2px; }}
.cb-progress progress {{ background: {accent}; min-height: 2px; }}
.cb-progress trough {{ background: transparent; border: none; min-height: 2px; }}

/* ---- tabs ---- */
notebook.cb-tabs > header {{
    background: {bar};
    border-bottom: 1px solid {line};
    padding: 0 4px;
}}
notebook.cb-tabs > header > tabs > tab {{
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    color: {dim};
    padding: 5px 10px;
    margin: 2px 1px 0 1px;
    border-radius: 6px 6px 0 0;
    font-size: 0.88em;
    min-height: 0;
}}
notebook.cb-tabs > header > tabs > tab:checked {{
    background: {tab_active};
    border-bottom-color: {accent};
    color: {text};
}}
notebook.cb-tabs > header > tabs > tab:hover {{ color: {text}; }}
.cb-tabclose {{
    background: transparent; border: none; box-shadow: none;
    padding: 0 2px; margin: 0; min-width: 14px; min-height: 14px;
    color: {dim};
}}
.cb-tabclose:hover {{ color: {warn}; }}

/* ---- "Claude is driving this tab" ----
   GTK3 has no animation we can rely on here, so the tell is static and loud
   rather than pulsing: an accent ring on the tab, and an accent frame around
   the whole window while that tab is the one on screen. Both use the same ink
   as every other Claude surface, so it reads as "the assistant", not "an
   error". */
.cb-tablabel.cb-agent {{
    background: {accent_soft};
    border: 1px solid {accent};
    border-radius: 5px;
    padding: 0 4px;
    margin: -1px -2px;
}}
.cb-root.cb-agent-window {{
    border: 2px solid {accent};
    background: {accent_soft};
}}

/* The drag handle between page and console. GTK gives a paned separator 1px by
   default, which is accurate and unhittable; 6px with a visible line reads as a
   grip and still lands under the pointer. */
.cb-split > separator {{
    background: {line};
    min-height: 6px;
    border: none;
}}
.cb-split > separator:hover {{ background: {accent}; }}

/* ---- the Claude console, docked at the bottom like an inspector ---- */
.cb-panel {{
    background: {panel};
    border-top: 1px solid {line};
}}

.cb-panel-head {{
    background: {bar};
    border-bottom: 1px solid {line};
    padding: 3px 6px;
}}

/* Mode selector: segmented pills, the active one carrying the accent. */
.cb-mode {{
    background: transparent;
    border: none;
    box-shadow: none;
    color: {dim};
    padding: 3px 11px;
    margin: 1px;
    border-radius: 999px;
    font-size: 0.86em;
    min-height: 0;
}}
.cb-mode:hover {{ color: {text}; background: {field_focus}; }}
.cb-mode:checked {{
    background: {accent};
    color: {on_accent};
    font-weight: 600;
}}
.cb-mode:checked:hover {{ background: {accent}; color: {on_accent}; }}

.cb-status {{ color: {dim}; font-size: 0.82em; padding: 0 8px; }}
.cb-status.busy {{ color: {accent}; }}
.cb-status.ok {{ color: {ok}; }}
.cb-status.warn {{ color: {warn}; }}

.cb-panel-view {{
    background: {panel};
    color: {text};
    padding: 8px 10px;
    font-family: monospace;
    font-size: 0.92em;
}}
.cb-panel-view text {{ background: {panel}; color: {text}; }}
.cb-panel-view text selection {{ background: {accent}; color: {on_accent}; }}

/* Prompt row: a console line, accent caret and chevron. */
.cb-prompt-row {{
    background: {bar};
    border-top: 1px solid {line};
    padding: 3px 6px;
}}
.cb-chevron {{
    color: {accent};
    font-family: monospace;
    font-weight: 700;
    padding: 0 4px 0 6px;
}}
.cb-prompt {{
    background: transparent;
    color: {text};
    border: none;
    box-shadow: none;
    padding: 5px 4px;
    font-family: monospace;
    caret-color: {accent};
}}
.cb-prompt:focus {{ background: transparent; border: none; box-shadow: none; }}
.cb-prompt selection {{ background: {accent}; color: {on_accent}; }}

.cb-panel-btn {{
    background: transparent;
    border: 1px solid {line};
    box-shadow: none;
    color: {dim};
    border-radius: 6px;
    padding: 2px 10px;
    margin: 0 2px;
    font-size: 0.84em;
    min-height: 0;
}}
.cb-panel-btn:hover {{ color: {text}; border-color: {dim}; }}
.cb-panel-btn:disabled {{ color: {line}; border-color: {line}; }}
.cb-panel-btn.stop:hover {{ color: {warn}; border-color: {warn}; }}
"""


def palette(dark: bool) -> dict:
    """The raw colours, for widgets that cannot be styled with CSS
    (GtkTextTag takes colours directly, not style classes)."""
    return dict(_DARK if dark else _LIGHT)


def css(dark: bool) -> bytes:
    return _TEMPLATE.format(**palette(dark)).encode()
