"""The visual language of the browser, in one file.

Chrome stays minimal -- one 40px bar -- but not colourless. There are three
themes, chosen by name (`CB_THEME`, `--theme`), never by a boolean:

  dark, light   Claude's warm neutrals and coral accent. Unchanged.
  phosphor      a near-black, cool-cast HUD: cyan phosphor structure, hairline
                rules, bracketed fields, one static scanline gradient.

Colour is used for meaning, never decoration, and it is spent on two separate
axes -- which is the one thing to understand before editing a palette:

  accent   *chrome* state: focus, the active tab, load progress, selection,
           a private window. In dark/light this is Claude's coral; in phosphor
           it is cyan.
  agent    *Claude* state: the AI buttons, "Claude is driving this tab", the
           busy status. Always coral/amber, in every theme.
  ok       a finished run
  warn     something failed, and why

The two-accent split exists because the agent's on-page cursor is amber and
cannot be re-themed -- it is drawn into the page, not the chrome. If chrome
focus and agent activity shared an ink, then in phosphor either the cursor
would no longer match its own chrome, or "Claude is doing something" would
become the same green-cyan as "this text field has focus", and the one signal
that must never be missed would be the most common colour on screen. So
structure gets the phosphor, and Claude keeps the warm ink it already owns.
In dark/light `agent` is simply the same coral `accent` already was, so those
two themes keep exactly the colours they had.

Tokens (the same keys in every theme, so a template written against one theme
renders in all three):

  bg bar panel field field_focus tab_active   surfaces, back to front
  line          the load-bearing 1px rule; >= 3:1 on every surface
  edge          a decorative structural line -- HUD brackets, corner marks
  text dim      body ink (>= 4.5:1) and secondary ink (>= 3:1)
  accent accent_soft on_accent    chrome state
  agent agent_soft on_agent       Claude state
  ok warn       outcome
  grid          ink of the static texture; equals `bg` where there is none
  mono          the chrome font chain -- only faces installed here
  name          the theme's own name, for templates that branch on it

`mono` is for chrome labels (tabs, omnibox, status, menu) only. Web page
content is never restyled by this file, and never gets a font from it.
"""

# Only faces `fc-list` actually reports on this machine, most-preferred first.
# No downloaded fonts, ever: this browser has no network at build time and a
# missing family silently falls back to whatever GTK picks, which is a
# proportional sans and destroys the whole point of a monospaced chrome.
_MONO = ('"DejaVu Sans Mono", "Liberation Mono", "Noto Sans Mono", '
         '"Noto Mono", "Nimbus Mono PS", monospace')

# Claude's warm neutrals and coral accent. The dark surfaces are warm-tinted
# (note the red channel leads) so the orange sits on them without clashing.
_DARK = {
    "name": "dark",
    "bg": "#1f1e1d",
    "bar": "#262624",
    "panel": "#232221",
    "line": "#3a3937",
    "edge": "#3a3937",
    "text": "#f0eee6",
    "dim": "#9a968c",
    "field": "#2a2927",
    "field_focus": "#302e2c",
    "accent": "#d97757",       # Claude coral
    "accent_soft": "#4a2e24",  # accent at low alpha, pre-blended for GTK3
    "on_accent": "#1f1e1d",
    "agent": "#d97757",        # same ink: here chrome and Claude agree
    "agent_soft": "#4a2e24",
    "on_agent": "#1f1e1d",
    "ok": "#7fb069",
    "warn": "#e0894f",
    "tab_active": "#2f2d2b",
    "grid": "#1f1e1d",         # == bg: this theme draws no texture
    "mono": _MONO,
}

# On white, the coral has to darken to stay readable as text (#d97757 is only
# ~2.9:1 on white; this is ~4.6:1). Same hue, different job.
_LIGHT = {
    "name": "light",
    "bg": "#ffffff",
    "bar": "#faf9f5",
    "panel": "#f7f5ef",
    "line": "#e5e1d8",
    "edge": "#e5e1d8",
    "text": "#3d3d3a",
    "dim": "#6f6c63",
    "field": "#ffffff",
    "field_focus": "#ffffff",
    "accent": "#b8532f",
    "accent_soft": "#f6e4dc",
    "on_accent": "#ffffff",
    "agent": "#b8532f",
    "agent_soft": "#f6e4dc",
    "on_agent": "#ffffff",
    "ok": "#4a7c3f",
    "warn": "#a8541f",
    "tab_active": "#ffffff",
    "grid": "#ffffff",
    "mono": _MONO,
}

# Phosphor: a cool near-black instrument panel.
#
# The surfaces are blue-led rather than red-led (the exact inverse of _DARK) and
# stop short of #000 -- pure black gives a 1px hairline nothing to sit on, and
# on an LCD it reads as a hole rather than as a surface. Everything here was
# picked against the contrast floor, not by eye: `text` is 17:1 on `bg`, `dim`
# 8.8:1, `line` 4.1:1, and each of accent/agent/ok/warn clears 6.5:1 on every
# surface it is ever painted on -- see tests/test_style.py, which computes them.
#
# Four hues, far enough apart to be told apart at 12px and at a glance:
# cyan (chrome), amber (Claude), green (done), red (failed).
_PHOSPHOR = {
    "name": "phosphor",
    "bg": "#04070b",
    "bar": "#070c12",
    "panel": "#060a10",
    "line": "#4a7686",         # 4.06:1 on bg -- a hairline you can still see
    "edge": "#1f7288",         # deeper cyan: brackets and corner marks
    "text": "#d6f5ea",
    "dim": "#8fb3ab",
    "field": "#050a10",
    "field_focus": "#08131b",
    "accent": "#38dbff",       # phosphor cyan: focus, structure, chrome state
    "accent_soft": "#0b252d",  # accent at 14% over bg, pre-blended for GTK3
    "on_accent": "#04070b",
    "agent": "#ffb454",        # Claude's ink here: amber, matching the cursor
    "agent_soft": "#271f15",
    "on_agent": "#04070b",
    "ok": "#4ee88a",
    "warn": "#ff6b6b",
    "tab_active": "#0c1620",
    "grid": "#38dbff",         # the scanline is the accent at ~3% alpha
    "mono": _MONO,
}

THEMES = {"dark": _DARK, "light": _LIGHT, "phosphor": _PHOSPHOR}

#: In the order they are offered on cb:settings and on `--theme`.
THEME_NAMES = ("dark", "light", "phosphor")

# What an install with no opinion gets. Phosphor rather than the desktop's
# dark/light, because this browser is not trying to disappear into the desktop
# -- and "follow the desktop" stays one pick away on cb:settings for anyone who
# would rather it did.
DEFAULT_THEME = "phosphor"

#: Which themes want the dark stock GTK widgets underneath (scrollbars, the
#: file chooser, anything this sheet does not repaint).
DARK_THEMES = ("dark", "phosphor")

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
    {mo}
}}
.cb-nav button:hover {{ color: {text}; background: {field_focus}; border-color: {line}; }}
.cb-nav button:active {{ color: {accent}; background: {accent_soft}; border-color: transparent; }}
.cb-nav button:disabled {{ color: {line}; background: transparent; border-color: transparent; }}

/* The Claude actions get the agent ink, so they read as a group -- and as the
   same program as the amber cursor Claude draws on the page. */
.cb-nav button.cb-ai {{ color: {agent}; }}
.cb-nav button.cb-ai:hover {{ background: {agent_soft}; color: {agent}; }}

/* The bookmark star: lit means saved. The one control here whose colour is
   state rather than decoration. */
.cb-nav button.cb-star.on {{ color: {accent}; }}
.cb-nav button.cb-star.on:hover {{ background: {accent_soft}; }}

/* ---- private tabs ----
   A private window must be obvious at a glance without being loud: a dashed
   accent underline on the bar, and a badge on the tab itself. Chrome state, so
   chrome ink -- a private window is not Claude doing something. */
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

/* ---- VPN Mode ----
   Chrome ink, not agent ink: where the browser's packets go is a property of
   the window, the same axis as "this is a private tab" -- it is not Claude
   doing something, and painting it amber would put it in the one colour that
   has to keep meaning only that. It is told apart from the private underline
   by being a filled pill carrying text (the exit address) rather than a rule.

   It is only ever visible while the mode is engaged, so it costs nothing when
   it has nothing to say. `bad` is the failed state, and it is the loud one on
   purpose: in that state the browser has stopped loading pages.

   Every selector here is `.cb-nav button.cb-vpnpill` and not `.cb-vpnpill`,
   because the pill lives in the toolbar's right-hand `.cb-nav` box and
   `.cb-nav button` above is a *more specific* selector than a bare class. A
   plain `.cb-vpnpill` therefore lost every property the nav rule also sets --
   which is not a rule that fails loudly, it is a pill that renders as ordinary
   dim toolbar text with no box around it, and only in the two themes whose
   `dim` happens to look like ordinary text. */
.cb-nav button.cb-vpnpill {{
    background: {accent_soft};
    background-image: none;
    box-shadow: none;
    color: {accent};
    border: 1px solid {accent};
    border-radius: 6px;
    padding: 1px 7px;
    margin: 0 3px;
    min-height: 0;
    min-width: 0;
    font-size: 0.76em;
    font-weight: 700;
}}
.cb-nav button.cb-vpnpill:hover {{
    background: {accent};
    color: {on_accent};
    border-color: {accent};
}}
/* Not yet proven. Deliberately quiet -- the proxy is already applied, so this
   is "checking", not "warning". */
.cb-nav button.cb-vpnpill.wait {{
    background: transparent;
    color: {dim};
    border-color: {line};
}}
.cb-nav button.cb-vpnpill.wait:hover {{ background: {field_focus}; color: {text}; }}
.cb-nav button.cb-vpnpill.bad {{
    background: transparent;
    color: {warn};
    border-color: {warn};
}}
.cb-nav button.cb-vpnpill.bad:hover {{ background: {warn}; color: {bg}; }}

/* ---- omnibox ---- */
.cb-omnibox {{
    background: {field};
    color: {text};
    border: 1px solid {line};
    border-radius: 9px;
    padding: 6px 12px;
    margin: 0 6px;
    caret-color: {accent};
    {mo_field}
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
   it does not shout: the surface is the same neutral as every other panel and
   the accent appears exactly twice -- a 3px spine on the left edge, and the
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
    {mo_field}
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
    {mo}
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
    {mo}
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
    font-family: {mono};
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
    {mo}
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
    {mo}
}}
.cb-tabclose:hover {{ color: {text}; background: alpha({text}, 0.10); }}

/* ---- "Claude is driving this tab" ----
   GTK3 has no animation we can rely on here, so the tell is static and loud
   rather than pulsing: an agent-ink ring on the tab, and a frame around the
   whole window while that tab is the one on screen. Both use the same ink as
   the cursor Claude draws into the page, so it reads as "the assistant", not
   "an error" and not "something has focus". */
.cb-tablabel.cb-agent {{
    background: {agent_soft};
    border: 1px solid {agent};
    border-radius: 5px;
    padding: 0 4px;
    margin: -1px -2px;
}}
.cb-root.cb-agent-window {{
    border: 2px solid {agent};
    background: {agent_soft};
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

/* Persona selector: a quiet combo that reads as part of the mode row rather
   than as a form control dropped into it. The button inside carries the theme's
   bevel, so its gradient and inset shadow are cleared too -- see the note in
   CLAUDE.md about "flat" buttons keeping a ghost outline. */
.cb-persona, .cb-persona button {{
    background: transparent;
    background-image: none;
    box-shadow: none;
    border: 1px solid {line};
    color: {dim};
    border-radius: 6px;
    padding: 0 4px;
    font-size: 0.82em;
    min-height: 0;
}}
.cb-persona button:hover {{ color: {text}; border-color: {dim}; }}

/* `busy` is Claude working, so it takes the agent ink; ok/warn are the outcome
   of that work and keep their own. */
.cb-status {{ color: {dim}; font-size: 0.82em; padding: 0 8px; }}
.cb-status.busy {{ color: {agent}; }}
.cb-status.ok {{ color: {ok}; }}
.cb-status.warn {{ color: {warn}; }}

.cb-panel-view {{
    background: {panel};
    color: {text};
    padding: 8px 10px;
    font-family: {mono};
    font-size: 0.92em;
}}
.cb-panel-view text {{ background: {panel}; color: {text}; }}
.cb-panel-view text selection {{ background: {accent}; color: {on_accent}; }}

/* Prompt row: a console line. The chevron is where you type *to Claude*, so it
   carries the agent ink; the caret is ordinary text focus and does not. */
.cb-prompt-row {{
    background: {bar};
    border-top: 1px solid {line};
    padding: 3px 6px;
}}
.cb-chevron {{
    color: {agent};
    font-family: {mono};
    font-weight: 700;
    padding: 0 4px 0 6px;
}}
.cb-prompt {{
    background: transparent;
    color: {text};
    border: none;
    box-shadow: none;
    padding: 5px 4px;
    font-family: {mono};
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

# ---------------------------------------------------------------------------
# The HUD layer.
#
# Appended after the base sheet, for the phosphor theme only, and every rule in
# it is an override of one already above. Written this way rather than as a
# second full template so that dark and light keep exactly the shape they have
# today -- picking phosphor is meant to be a visible choice, not a silent
# redesign of the other two -- and so there is one place to read what "HUD"
# actually means here:
#
#   * square corners. A radius is the single loudest 2010s-app tell, and a HUD
#     is drawn with a plotter, not a rounded-rect API.
#   * brackets instead of boxes. The focused field is held between two 2px
#     accent uprights with hairlines top and bottom -- "[ text ]" -- which reads
#     as an instrument slot rather than as a form input, and needs no
#     pseudo-elements (GTK3 has none).
#   * monospaced, letterspaced micro-labels. GTK3 has no `text-transform`, so
#     the strings themselves stay as the widgets set them; the tell is the face
#     and the tracking.
#   * one static scanline. `repeating-linear-gradient` at 3% alpha, painted
#     once into each chrome strip and cached by GTK like any other background.
#     Not on `window`/`.cb-root`: the WebView covers those entirely, so it would
#     be pure cost for pixels nobody sees -- and never as an animation, which on
#     two cores is a frame budget spent on decoration.
# ---------------------------------------------------------------------------
_HUD = """
/* Scanline. One declaration, reused: 1px of accent at 3% every 3px. At 3% it is
   texture rather than pattern -- it should be noticeable on a large flat strip
   and invisible behind text. */
.cb-bar, .cb-panel-head, .cb-prompt-row, .cb-findbar, .cb-pwbar,
notebook.cb-tabs > header {{
    background-image: repeating-linear-gradient(
        to bottom,
        alpha({grid}, 0.030) 0px, alpha({grid}, 0.030) 1px,
        transparent 1px, transparent 3px);
}}
/* The bar's underline is the top rule of the instrument: hairline, plus one
   faint accent line under it so the chrome sits on a lit edge. */
.cb-bar {{
    border-bottom: 1px solid {edge};
    box-shadow: inset 0 -2px 0 -1px alpha({accent}, 0.22);
}}

/* Square everything the base sheet rounded. */
.cb-nav button, .cb-pwbtn, .cb-findbtn, .cb-findtoggle, .cb-tabclose,
.cb-menuitem, .cb-priv-badge, .cb-panel-btn, .cb-persona,
.cb-persona button, .cb-tablabel.cb-agent {{ border-radius: 0; }}
.cb-nav button.cb-vpnpill {{ border-radius: 0; }}
.cb-mode {{ border-radius: 0; }}
notebook.cb-tabs > header > tabs > tab {{ border-radius: 0; }}
popover.cb-menu, popover.cb-menu > arrow {{ border-radius: 0; }}

/* Chrome type: monospaced, tracked out, small. Never the page -- these are all
   chrome nodes. */
.cb-nav button, .cb-pwbtn, .cb-findbtn, .cb-findtoggle, .cb-findcount,
.cb-menuitem, .cb-menuhead, .cb-status, .cb-priv-badge, .cb-mode,
.cb-panel-btn, .cb-persona button, .cb-pwbar label, .cb-vpnpill,
notebook.cb-tabs > header > tabs > tab {{
    font-family: {mono};
}}
/* The micro-labels: the ones that are a legend on an instrument rather than a
   sentence. Tracking is what makes 11px monospace read as engraved. */
.cb-menuhead, .cb-status, .cb-findcount, .cb-priv-badge, .cb-mode,
.cb-panel-btn, .cb-findtoggle, .cb-vpnpill {{
    letter-spacing: 1px;
}}
/* The exit address is the payload of this pill, so it gets the glow the
   phosphor sheet gives everything else that is live. Same specificity note as
   the base rules: `.cb-nav button` would otherwise beat a bare class. */
.cb-nav button.cb-vpnpill {{ box-shadow: 0 0 7px alpha({accent}, 0.26); }}
.cb-nav button.cb-vpnpill.wait {{ box-shadow: none; }}
.cb-nav button.cb-vpnpill.bad {{ box-shadow: 0 0 7px alpha({warn}, 0.30); }}
.cb-menuhead {{ letter-spacing: 2px; }}
notebook.cb-tabs > header > tabs > tab {{ letter-spacing: 0.5px; font-size: 0.84em; }}

/* ---- the omnibox as a bracketed slot ---- */
.cb-omnibox {{
    border-radius: 0;
    border: 1px solid {line};
    border-left: 2px solid {edge};
    border-right: 2px solid {edge};
    font-family: {mono};
    padding: 6px 10px;
}}
/* Focus: the brackets light, and the field gets a real glow rather than a
   flat ring -- 1px hard edge plus 9px of bloom, which is the CRT tell. */
.cb-omnibox:focus {{
    border-color: {line};
    border-left-color: {accent};
    border-right-color: {accent};
    box-shadow: 0 0 0 1px alpha({accent}, 0.45), 0 0 9px alpha({accent}, 0.30);
}}
entry.cb-findentry {{
    border-radius: 0;
    border-left: 2px solid {edge};
    border-right: 2px solid {edge};
    font-family: {mono};
}}
entry.cb-findentry:focus {{
    border-color: {line};
    border-left-color: {accent};
    border-right-color: {accent};
    box-shadow: 0 0 0 1px alpha({accent}, 0.45), 0 0 9px alpha({accent}, 0.30);
}}
entry.cb-findentry.cb-find-miss,
entry.cb-findentry.cb-find-miss:focus {{
    border-color: {warn};
    box-shadow: 0 0 0 1px alpha({warn}, 0.45), 0 0 9px alpha({warn}, 0.30);
}}

/* The active tab: a lit underline plus the same bloom, so the eye finds it in
   a row of dim monospace without needing a filled shape. */
notebook.cb-tabs > header > tabs > tab:checked {{
    box-shadow: 0 0 8px alpha({accent}, 0.22);
}}

/* Buttons read as switch positions: a hairline that lights on hover. */
.cb-nav button:hover {{ border-color: {edge}; }}
.cb-nav button:active {{ box-shadow: 0 0 7px alpha({accent}, 0.28); }}
.cb-nav button.cb-ai:hover {{ box-shadow: 0 0 7px alpha({agent}, 0.28); }}

/* The menu is a panel bolted to the bar, not a floating pill: square, a
   hairline in the structural cyan, and a hard shadow instead of a soft one. */
popover.cb-menu {{
    border: 1px solid {edge};
    box-shadow: 0 6px 22px alpha(#000000, 0.55);
}}
.cb-menuhead-gap {{ border-top: 1px solid {edge}; }}

/* Claude working: amber, and the only glow on screen that is not cyan. That is
   the whole point of the split -- at a glance, colour alone says whether the
   machine is waiting for you or for Claude. */
.cb-tablabel.cb-agent {{ box-shadow: 0 0 8px alpha({agent}, 0.35); }}
.cb-root.cb-agent-window {{ border-color: {agent}; }}

/* The console keeps a plain surface: it is a field of text, and a scanline
   behind it would fight the lines of the text itself. Only its chrome (head,
   prompt row) carries the texture. */
.cb-panel {{ border-top: 1px solid {edge}; }}
.cb-panel-btn {{ border-color: {edge}; }}
.cb-split > separator {{
    background: {bar};
    border-top: 1px solid {edge};
    border-bottom: 1px solid {edge};
}}
.cb-chevron {{ text-shadow: 0 0 8px alpha({agent}, 0.45); }}
"""

# The transitions, as tokens rather than literals, so reduced motion removes
# them instead of overriding them: a later `transition: none` rule would lose to
# any earlier selector with higher specificity, and GTK3 has no `!important`
# escape worth relying on. An empty string here means the property is simply
# never declared.
_MOTION = {
    "mo": ("transition: background 120ms ease, color 120ms ease, "
           "border-color 120ms ease;"),
    "mo_field": "transition: border-color 120ms ease, box-shadow 120ms ease;",
}
_STILL = {"mo": "", "mo_field": ""}


def resolve(theme) -> str:
    """The name of the theme to use for `theme`.

    Accepts a name, or None/"" meaning "the caller has no opinion" -- which
    lands on DEFAULT_THEME rather than raising, because a typo in `CB_THEME`
    must not be able to stop the browser from starting.
    """
    name = (theme or "").strip().lower()
    return name if name in THEMES else DEFAULT_THEME


def is_dark(theme) -> bool:
    """Should the stock GTK widgets under this sheet be the dark ones?"""
    return resolve(theme) in DARK_THEMES


def palette(theme) -> dict:
    """The raw colours, for widgets that cannot be styled with CSS
    (GtkTextTag takes colours directly, not style classes) and for the `cb:`
    pages and the Claude panel, which render HTML against the same tokens."""
    return dict(THEMES[resolve(theme)])


def css(theme, motion: bool = True) -> bytes:
    """The GTK3 sheet for `theme`.

    `motion=False` strips every transition. That state is not guessable from
    inside this module -- it is `perf.light_enabled()` and GTK's own
    `gtk-enable-animations`, both of which live outside it -- so the caller
    reads it and passes it in. Importing perf here would drag `gi` into a module
    that is otherwise pure data and testable without a display.
    """
    name = resolve(theme)
    tokens = dict(THEMES[name], **(_MOTION if motion else _STILL))
    sheet = _TEMPLATE + (_HUD if name == "phosphor" else "")
    return sheet.format(**tokens).encode()
