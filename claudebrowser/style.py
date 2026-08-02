"""The entire visual language of the browser, in one string.

Deliberately small. Chrome is one 40px bar; everything else is page.
Colors are defined once here and nowhere else.
"""

# Two palettes, one structure. GTK3 has no prefers-color-scheme, so we pick at
# startup from the GTK theme's own dark preference and swap the whole block.
_DARK = {
    "bg": "#16171a",
    "bar": "#1c1e22",
    "line": "#292c31",
    "text": "#e6e8ea",
    "dim": "#8b9096",
    "field": "#232629",
    "field_focus": "#282c30",
    "accent": "#6aa9ff",
    "tab_active": "#2b2f34",
}

_LIGHT = {
    "bg": "#ffffff",
    "bar": "#f6f7f8",
    "line": "#e2e5e9",
    "text": "#1a1c1e",
    "dim": "#6b7178",
    "field": "#ffffff",
    "field_focus": "#ffffff",
    "accent": "#1a6fe0",
    "tab_active": "#ffffff",
}

_TEMPLATE = """
window, .cb-root {{ background: {bg}; }}

/* The one bar. 40px, no gradients, no shadows, no window title. */
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
    margin: 0;
    min-width: 20px;
    min-height: 20px;
}}
.cb-nav button:hover {{ color: {text}; background: {field_focus}; border-radius: 5px; }}
.cb-nav button:disabled {{ color: {line}; }}

/* Omnibox: a text field that does not look like a text field until focused. */
.cb-omnibox {{
    background: {field};
    color: {text};
    border: 1px solid {line};
    border-radius: 7px;
    padding: 5px 11px;
    margin: 0 4px;
    caret-color: {accent};
}}
.cb-omnibox:focus {{
    background: {field_focus};
    border-color: {accent};
    box-shadow: none;
}}
.cb-omnibox selection {{ background: {accent}; color: {bg}; }}

/* Progress: a 2px hairline under the bar, not a widget. */
.cb-progress {{
    background: transparent;
    min-height: 2px;
}}
.cb-progress progress {{
    background: {accent};
    min-height: 2px;
}}
.cb-progress trough {{ background: transparent; border: none; min-height: 2px; }}

/* Tabs. Hidden entirely when there is only one. */
notebook.cb-tabs > header {{
    background: {bar};
    border-bottom: 1px solid {line};
    padding: 0 4px;
}}
notebook.cb-tabs > header > tabs > tab {{
    background: transparent;
    border: none;
    color: {dim};
    padding: 5px 10px;
    margin: 3px 1px;
    border-radius: 6px;
    font-size: 0.88em;
    min-height: 0;
}}
notebook.cb-tabs > header > tabs > tab:checked {{
    background: {tab_active};
    color: {text};
}}
notebook.cb-tabs > header > tabs > tab:hover {{ color: {text}; }}
.cb-tabclose {{
    background: transparent;
    border: none;
    box-shadow: none;
    padding: 0 2px;
    margin: 0;
    min-width: 14px;
    min-height: 14px;
    color: {dim};
}}
.cb-tabclose:hover {{ color: {text}; }}

/* The ask-Claude panel: same bar treatment, docked at the bottom. */
.cb-ask {{
    background: {bar};
    border-top: 1px solid {line};
    padding: 6px;
}}
.cb-ask-view {{
    background: {bar};
    color: {text};
    padding: 6px 10px;
}}
.cb-ask-view text {{ background: {bar}; color: {text}; }}
.cb-hint {{ color: {dim}; font-size: 0.85em; padding: 0 6px; }}
"""


def css(dark: bool) -> bytes:
    return _TEMPLATE.format(**(_DARK if dark else _LIGHT)).encode()
