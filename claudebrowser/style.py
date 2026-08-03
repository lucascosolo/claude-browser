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
    border: 1px solid transparent;
    box-shadow: none;
    color: {dim};
    padding: 5px 8px;
    margin: 0 1px;
    min-width: 20px;
    min-height: 20px;
    border-radius: 7px;
    transition: background 120ms ease, color 120ms ease, border-color 120ms ease;
}}
.cb-nav button:hover {{ color: {text}; background: {field_focus}; border-color: {line}; }}
.cb-nav button:active {{ color: {accent}; background: {accent_soft}; border-color: transparent; }}
.cb-nav button:disabled {{ color: {line}; background: transparent; border-color: transparent; }}

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
    border-radius: 9px;
    padding: 6px 12px;
    margin: 0 6px;
    caret-color: {accent};
    transition: border-color 120ms ease, box-shadow 120ms ease;
}}
/* A focus ring rather than a hard accent outline: 3px of very transparent
   accent reads as "this is live" without redrawing the box in a new colour
   every time the address bar is clicked, which is most of the time. */
.cb-omnibox:focus {{
    background: {field_focus};
    border-color: {accent};
    box-shadow: 0 0 0 3px alpha({accent}, 0.16);
}}
.cb-omnibox selection {{ background: {accent}; color: {on_accent}; }}

/* ---- load progress: a 2px accent hairline ---- */
.cb-progress {{ background: transparent; min-height: 2px; }}
.cb-progress progress {{ background: {accent}; min-height: 2px; }}
.cb-progress trough {{ background: transparent; border: none; min-height: 2px; }}

/* ---- the save-password bar ----
   Sits under the toolbar and above the page. It is a prompt, not an alert, so
   it does not shout: the surface is the same warm neutral as every other panel
   and the accent appears exactly twice -- a 3px spine on the left edge, and the
   one button that actually does something. An accent-washed background with a
   flat accent button was the first attempt and read as an error banner; colour
   spent everywhere buys no emphasis anywhere. */
.cb-pwbar {{
    background: {panel};
    border-bottom: 1px solid {line};
    border-left: 3px solid {accent};
    padding: 9px 8px 9px 13px;
}}
.cb-pwbar label {{ color: {text}; font-size: 0.94em; }}

/* The find bar. Deliberately the same furniture as the password bar -- both are
   transient strips that appear between the toolbar and the page, and a browser
   that invents a new visual idiom per feature stops reading as one program. */
.cb-findbar {{
    background: {panel};
    border-bottom: 1px solid {line};
    padding: 6px 8px 6px 11px;
}}
/* `entry.cb-findentry`, not `.cb-findentry`: the stock theme styles `entry:focus`
   and wins on specificity against a bare class, which paints the stock blue
   focus border straight over the accent. Naming the element takes it back --
   the same reason the omnibox spends a box-shadow rather than a border here. */
entry.cb-findentry {{
    background: {field};
    color: {text};
    border: 1px solid {line};
    border-radius: 7px;
    padding: 3px 9px;
    min-height: 0;
    outline: none;
    caret-color: {accent};
    transition: border-color 120ms ease, box-shadow 120ms ease;
}}
entry.cb-findentry:focus {{
    background: {field_focus};
    border-color: {accent};
    box-shadow: 0 0 0 3px alpha({accent}, 0.16);
}}
entry.cb-findentry selection {{ background: {accent}; color: {on_accent}; }}
/* Nothing found: tint the box rather than pop a dialog. The answer is already
   on screen and only needs to be legible. Has to beat the focus rule above,
   because the entry is focused for the whole time it is showing a miss. */
entry.cb-findentry.cb-find-miss,
entry.cb-findentry.cb-find-miss:focus {{
    color: {warn};
    border-color: {warn};
    box-shadow: 0 0 0 3px alpha({warn}, 0.16);
}}
.cb-findcount {{ color: {dim}; font-size: 0.86em; padding: 0 4px; }}
.cb-findbtn {{
    background: transparent;
    background-image: none;
    box-shadow: none;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 2px 5px;
    min-height: 0;
    min-width: 0;
    color: {dim};
}}
.cb-findbtn:hover {{ color: {text}; background: {field_focus}; }}
.cb-findtoggle {{
    background: transparent;
    background-image: none;
    box-shadow: none;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 2px 8px;
    min-height: 0;
    color: {dim};
    font-size: 0.86em;
    font-weight: 600;
}}
.cb-findtoggle:hover {{ color: {text}; background: {field_focus}; }}
.cb-findtoggle:checked {{
    background: {accent_soft};
    color: {accent};
    border-color: {accent};
}}

/* A tab whose page was dropped to reclaim memory. Dimmed, not badged: it is
   still that tab, and a marker would make an invisible optimisation look like
   a failure. */
.cb-tab-dim label {{ opacity: 0.55; }}

/* background-image and box-shadow both have to be cleared explicitly: the stock
   theme paints its button bevel with a gradient image and an inset shadow, and
   setting only `background` leaves both of them showing through as a ghost
   outline around what is supposed to be a flat text button. */
.cb-pwbtn {{
    background: transparent;
    background-image: none;
    box-shadow: none;
    border: 1px solid transparent;
    border-radius: 7px;
    padding: 4px 13px;
    margin: 0 1px;
    min-height: 0;
    color: {dim};
    font-size: 0.92em;
    outline: none;
    transition: background 120ms ease, color 120ms ease, border-color 120ms ease;
}}
.cb-pwbtn:hover {{ color: {text}; background: {field_focus}; border-color: {line}; }}

/* The primary. Same ink as before -- what was wrong was the shape, not the
   hue: a flat rectangle of saturated coral with no radius, no padding and no
   depth. Gradient, hairline and a 1px shadow are what make it read as a raised
   control rather than a coloured div. */
.cb-pwbtn-go {{
    background: linear-gradient(to bottom, shade({accent}, 1.10), {accent});
    border: 1px solid shade({accent}, 0.90);
    color: {on_accent};
    font-weight: 600;
    box-shadow: 0 1px 2px alpha(#000000, 0.16);
}}
.cb-pwbtn-go:hover {{
    background: linear-gradient(to bottom, shade({accent}, 1.17), shade({accent}, 1.06));
    border-color: shade({accent}, 0.94);
    color: {on_accent};
}}
.cb-pwbtn-go:active {{
    background: shade({accent}, 0.95);
    box-shadow: inset 0 1px 2px alpha(#000000, 0.20);
}}

/* ---- the menu ----
   A card, not a list of system menu items: 10px radius, one soft shadow, and
   sections separated by a quiet uppercase heading rather than a rule. Rows are
   full-width targets so the whole strip lights up, which is what makes a menu
   feel responsive rather than fiddly. */
/* The card is painted on the popover node itself. GTK4 splits a popover into
   `popover > contents` and styling that node is the modern advice; GTK3 has no
   such node, so those rules match nothing and the menu renders as floating text
   over the page with no surface behind it. */
popover.cb-menu {{
    background: {panel};
    border: 1px solid {line};
    border-radius: 11px;
    padding: 0;
    box-shadow: 0 10px 28px alpha(#000000, 0.22);
}}
popover.cb-menu > arrow {{
    background: {panel};
    border: 1px solid {line};
}}
.cb-menucard {{ padding: 7px; min-width: 258px; }}

.cb-menuhead {{
    color: {dim};
    font-size: 0.72em;
    font-weight: 700;
    padding: 5px 10px 4px 10px;
}}
.cb-menuhead-gap {{ margin-top: 5px; border-top: 1px solid {line}; padding-top: 9px; }}

.cb-menuitem {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 7px 10px;
    min-height: 0;
    color: {text};
    outline: none;
    transition: background 110ms ease, color 110ms ease;
}}
/* GTK gives the first row keyboard focus as soon as the popover opens, and the
   default focus ring reads as "already selected" on a menu nobody has touched. */
.cb-menuitem:focus {{ outline: none; border-color: transparent; }}
.cb-menuitem:hover {{ background: {accent_soft}; color: {accent}; }}
.cb-menuitem:active {{ background: shade({accent_soft}, 0.94); }}
.cb-menuitem image {{ color: {dim}; }}
.cb-menuitem:hover image {{ color: {accent}; }}

/* The shortcut, set back so it reads as a footnote to the row and never
   competes with the label for the eye. */
.cb-accel {{
    color: {dim};
    font-size: 0.80em;
    font-family: monospace;
    padding-left: 14px;
}}
.cb-menuitem:hover .cb-accel {{ color: alpha({accent}, 0.85); }}

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
    padding: 6px 11px;
    margin: 3px 1px 0 1px;
    border-radius: 8px 8px 0 0;
    font-size: 0.88em;
    min-height: 0;
    transition: background 120ms ease, color 120ms ease;
}}
notebook.cb-tabs > header > tabs > tab:checked {{
    background: {tab_active};
    border-bottom-color: {accent};
    color: {text};
}}
notebook.cb-tabs > header > tabs > tab:hover {{ color: {text}; background: {field_focus}; }}
notebook.cb-tabs > header > tabs > tab:checked:hover {{ background: {tab_active}; }}
.cb-tabclose {{
    background: transparent; border: none; box-shadow: none;
    padding: 0 3px; margin: 0; min-width: 16px; min-height: 16px;
    border-radius: 5px;
    color: {dim};
    transition: background 120ms ease, color 120ms ease;
}}
.cb-tabclose:hover {{ color: {text}; background: alpha({text}, 0.10); }}

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
