"""The Claude surface, rendered as HTML cards.

There is a full browser engine in this process already. Using a GtkTextView for
Claude's output meant a wall of monospace with no hierarchy; rendering into a
small WebView instead gets real cards, colour and typography for free, and costs
one extra view in a process we are already running.

Each answer, each agent step, each error is a card. Cards are scannable in a way
a transcript is not: you can find the one that failed without reading the rest.

The page exposes a tiny JS API the Python side drives:

    cb.clear()                     drop every card
    cb.card(id, kind, title)       add one; kind styles it
    cb.append(id, text)            stream text into its body
    cb.meta(id, text)              set the small footer line
    cb.step(id, text)              add an agent-step chip
    cb.done(id, kind)              restyle when finished
"""

import json


def page(palette):
    """The panel document. Colours are interpolated so it matches the GTK
    chrome exactly in both themes."""
    return _TEMPLATE % {
        "bg": palette["panel"],
        "card": palette["bar"],
        "line": palette["line"],
        "text": palette["text"],
        "dim": palette["dim"],
        "accent": palette["accent"],
        "accent_soft": palette["accent_soft"],
        "on_accent": palette["on_accent"],
        "ok": palette["ok"],
        "warn": palette["warn"],
    }


def call(fn, *args):
    """Build a JS call with JSON-encoded arguments.

    json.dumps is the escaping here for the same reason it is in extract.py --
    these strings are page content and model output, and must not be able to
    break out of the call.
    """
    return "cb.%s(%s)" % (fn, ",".join(json.dumps(a) for a in args))


_TEMPLATE = """<!doctype html>
<meta charset="utf-8">
<style>
  :root {
    --bg: %(bg)s; --card: %(card)s; --line: %(line)s; --text: %(text)s;
    --dim: %(dim)s; --accent: %(accent)s; --accent-soft: %(accent_soft)s;
    --on-accent: %(on_accent)s; --ok: %(ok)s; --warn: %(warn)s;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%%; }
  body {
    background: var(--bg);
    color: var(--text);
    font: 13.5px/1.55 system-ui, -apple-system, "Segoe UI", Cantarell, sans-serif;
    padding: 10px 12px 14px;
    overflow-x: hidden;
  }

  /* Empty state: the panel should explain itself, not sit blank. */
  .hint { color: var(--dim); font-size: 13px; max-width: 62ch; }
  .hint b { color: var(--text); font-weight: 600; }
  .hint kbd {
    background: var(--card); border: 1px solid var(--line); border-bottom-width: 2px;
    border-radius: 5px; padding: 1px 5px; font: 11px/1 monospace; color: var(--text);
  }

  .card {
    background: var(--card);
    border: 1px solid var(--line);
    border-left: 3px solid var(--accent);
    border-radius: 10px;
    padding: 9px 12px 10px;
    margin: 0 0 9px;
    animation: rise .16s ease-out;
  }
  @keyframes rise { from { opacity: 0; transform: translateY(4px); } }

  .card.error { border-left-color: var(--warn); }
  .card.ok    { border-left-color: var(--ok); }
  .card.you   { border-left-color: var(--line); background: transparent; }

  .card h3 {
    margin: 0 0 5px;
    font-size: 10.5px;
    font-weight: 700;
    letter-spacing: .07em;
    text-transform: uppercase;
    color: var(--dim);
    display: flex; align-items: center; gap: 7px;
  }
  .card.error h3 { color: var(--warn); }
  .card.ok h3 { color: var(--ok); }

  .body { white-space: pre-wrap; word-wrap: break-word; }
  .body:empty::after { content: "…"; color: var(--dim); }

  .meta { margin-top: 7px; font-size: 11.5px; color: var(--dim); }

  /* Agent steps: compact chips so a long run stays scannable. */
  .steps { margin-top: 7px; display: flex; flex-direction: column; gap: 3px; }
  .step {
    font: 11.5px/1.45 monospace;
    color: var(--dim);
    padding: 2px 8px;
    border-left: 2px solid var(--accent-soft);
  }
  .step.active { color: var(--accent); border-left-color: var(--accent); }

  code, pre { font-family: monospace; font-size: 12.5px; }
  pre { background: var(--bg); border: 1px solid var(--line); border-radius: 7px;
        padding: 8px 10px; overflow-x: auto; }
  a { color: var(--accent); }
</style>
<div id="root"></div>
<script>
(function () {
  var root = document.getElementById('root');
  var cards = {};

  function scroll() {
    // Only stick to the bottom if the user has not scrolled up to read.
    if (window.__pinned !== false) {
      window.scrollTo(0, document.body.scrollHeight);
    }
  }
  window.addEventListener('scroll', function () {
    var atBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 24;
    window.__pinned = atBottom;
  });

  window.cb = {
    clear: function () { root.innerHTML = ''; cards = {}; window.__pinned = true; },

    hint: function (html) { root.innerHTML = '<div class="hint">' + html + '</div>'; },

    card: function (id, kind, title) {
      var el = document.createElement('div');
      el.className = 'card' + (kind ? ' ' + kind : '');
      el.innerHTML = '<h3></h3><div class="steps"></div><div class="body"></div>'
                   + '<div class="meta"></div>';
      el.querySelector('h3').textContent = title || '';
      root.appendChild(el);
      cards[id] = el;
      scroll();
    },

    append: function (id, text) {
      var el = cards[id];
      if (!el) { this.card(id, '', ''); el = cards[id]; }
      el.querySelector('.body').textContent += text;
      scroll();
    },

    step: function (id, text) {
      var el = cards[id];
      if (!el) return;
      var steps = el.querySelector('.steps');
      var prev = steps.lastElementChild;
      if (prev) prev.classList.remove('active');
      var s = document.createElement('div');
      s.className = 'step active';
      s.textContent = text;
      steps.appendChild(s);
      scroll();
    },

    meta: function (id, text) {
      var el = cards[id];
      if (el) { el.querySelector('.meta').textContent = text; scroll(); }
    },

    done: function (id, kind) {
      var el = cards[id];
      if (!el) return;
      if (kind) el.className = 'card ' + kind;
      var active = el.querySelector('.step.active');
      if (active) active.classList.remove('active');
      scroll();
    }
  };
})();
</script>
"""


EMPTY_HINTS = {
    "ask": "<b>Ask</b> anything about the page you are on. Claude reads the "
           "rendered text, not the HTML source.",
    "tldr": "<b>TL;DR</b> summarizes the current page on demand. It never runs "
            "automatically, so you are not charged for pages you only glance at.",
    "research": "<b>Research</b> reads every open tab and synthesizes across them "
                "&mdash; good for comparing docs, pricing, or three takes on the "
                "same question. Open a few tabs first.",
    "agent": "<b>Command</b> gives Claude a goal and lets it drive this window: "
             "navigating, reading and clicking while you watch. Try "
             "<i>&ldquo;find the pricing page and list the tiers&rdquo;</i>.",
}


def empty_hint(mode):
    body = EMPTY_HINTS.get(mode, "")
    return body + '<div style="margin-top:10px">Press <kbd>Enter</kbd> to run · ' \
                  '<kbd>Esc</kbd> closes this panel.</div>'
