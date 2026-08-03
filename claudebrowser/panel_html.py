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

#: Where the phosphor overrides are spliced into the sheet. A comment rather
#: than a %-key so the slot is inert in the two themes that do not use it.
_HUD_SLOT = "/*@hud@*/"


def page(palette):
    """The panel document, in the theme the chrome is wearing.

    The whole palette goes in, not a hand-picked subset. This used to rebuild
    the dict key by key, which meant every token added to style.py -- `edge`,
    `grid`, `agent`, `mono` -- silently stopped at this function and the panel
    drifted out of the theme it was supposed to be part of. The only two
    remapped names are the ones the template calls something else: the panel's
    page surface is the chrome's `panel`, and a card sits on the chrome's `bar`.
    """
    tokens = dict(palette, bg=palette["panel"], card=palette["bar"])
    # The HUD text is spliced in *before* the single %-format pass, not
    # formatted separately and injected as a value: a value substituted by %
    # is not rescanned, so its own %(accent)s would reach the document raw.
    sheet = _TEMPLATE.replace(
        _HUD_SLOT, _HUD if palette.get("name") == "phosphor" else "")
    return sheet % tokens


def call(fn, *args):
    """Build a JS call with JSON-encoded arguments.

    json.dumps is the escaping here for the same reason it is in extract.py --
    these strings are page content and model output, and must not be able to
    break out of the call.
    """
    return "cb.%s(%s)" % (fn, ",".join(json.dumps(a) for a in args))


_TEMPLATE = r"""<!doctype html>
<meta charset="utf-8">
<style>
  :root {
    --bg: %(bg)s; --card: %(card)s; --line: %(line)s; --text: %(text)s;
    --dim: %(dim)s; --accent: %(accent)s; --accent-soft: %(accent_soft)s;
    --on-accent: %(on_accent)s; --ok: %(ok)s; --warn: %(warn)s;
    /* The rest of the contract. `agent` is the one that earns its keep here:
       almost everything in this panel *is* Claude working, so the ink that
       means that has to be reachable rather than approximated with --accent. */
    --edge: %(edge)s; --grid: %(grid)s; --field: %(field)s;
    --agent: %(agent)s; --agent-soft: %(agent_soft)s; --on-agent: %(on_agent)s;
    --mono: %(mono)s;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%%; }
  body {
    background: var(--bg);
    color: var(--text);
    font: 13.5px/1.55 system-ui, -apple-system, "Segoe UI", Cantarell, sans-serif;
    padding: 10px 12px 14px;
    overflow-x: hidden;
    /* The same static scanline the chrome carries, and the same trick: the ink
       is `grid` at 3%% (the `08` is the alpha byte), and `grid` equals `bg` in
       dark and light -- so there the gradient is the surface painted over
       itself at 3%%, which is exactly nothing, and needs no branch. Written
       against the raw token rather than var(--grid) because a hex alpha suffix
       cannot be appended to a var(). Never animated, because a permanently repainting
       background on two cores is a frame budget spent on decoration. */
    background-image: repeating-linear-gradient(
      to bottom, %(grid)s08 0, %(grid)s08 1px,
      transparent 1px, transparent 3px);
  }

  /* Empty state: the panel should explain itself, not sit blank. */
  .hint { color: var(--dim); font-size: 13px; max-width: 62ch; }
  .hint b { color: var(--text); font-weight: 600; }
  .hint kbd {
    background: var(--card); border: 1px solid var(--line); border-bottom-width: 2px;
    border-radius: 5px; padding: 1px 5px; font: 11px/1 monospace; color: var(--text);
  }

  /* A card is Claude answering, so the spine is the agent ink rather than the
     chrome accent -- the same amber as the cursor it draws into the page, and
     the same rule as everywhere else: chrome state is cyan/coral, Claude state
     is this. It stays agent-inked until `done()` restyles it to an outcome,
     which makes "still working" and "finished" a colour rather than a caption.
     Cards no longer animate in -- what was a 160ms keyframe per card is a
     repaint per card on a machine this exists to spare. */
  .card {
    background: var(--card);
    border: 1px solid var(--line);
    border-left: 3px solid var(--agent);
    border-radius: 10px;
    padding: 9px 12px 10px;
    margin: 0 0 9px;
  }

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

  /* The body is rendered markdown, so block spacing is the elements' own; a
     pre-wrap body would double every gap. Only <pre> keeps literal whitespace. */
  .body { word-wrap: break-word; overflow-wrap: anywhere; }
  /* The streaming tell: a card whose body has not received its first chunk
     yet. A block cursor in the agent ink rather than an ellipsis, because it
     says *which* program is about to write here, and it is one static glyph
     rather than a blinking one. */
  .body:empty::after { content: "\2588"; color: var(--agent); }
  .body > :first-child { margin-top: 0; }
  .body > :last-child { margin-bottom: 0; }
  .body p { margin: 0 0 7px; }
  .body h1, .body h2, .body h3, .body h4, .body h5, .body h6 {
    margin: 11px 0 5px; line-height: 1.3; font-weight: 650; display: block;
    text-transform: none; letter-spacing: 0; color: var(--text);
  }
  .body h1 { font-size: 1.3em; }
  .body h2 { font-size: 1.18em; }
  .body h3 { font-size: 1.08em; }
  .body h4, .body h5, .body h6 { font-size: 1em; color: var(--dim); }
  .body ul, .body ol { margin: 0 0 7px; padding-left: 1.35em; }
  .body li { margin: 1px 0; }
  .body blockquote {
    margin: 0 0 7px; padding: 1px 0 1px 10px;
    border-left: 2px solid var(--accent-soft); color: var(--dim);
  }
  .body pre { margin: 0 0 8px; white-space: pre; }
  .body code { background: var(--bg); border: 1px solid var(--line);
               border-radius: 4px; padding: 0 4px; }
  .body pre code { background: none; border: none; padding: 0; }
  .body hr { border: none; border-top: 1px solid var(--line); margin: 10px 0; }
  .body table {
    border-collapse: collapse; margin: 0 0 8px; font-size: 12.5px;
    display: block; overflow-x: auto; max-width: 100%%;
  }
  .body th, .body td {
    border: 1px solid var(--line); padding: 3px 8px; text-align: left;
    vertical-align: top;
  }
  .body th { background: var(--bg); font-weight: 650; }
  .body strong { font-weight: 650; color: var(--text); }
  .body del { color: var(--dim); }

  .meta { margin-top: 7px; font-size: 11.5px; color: var(--dim); }

  /* Agent steps: compact chips so a long run stays scannable. The one still
     running is Claude working, so it lights in the agent ink; the ones behind
     it are history and stay quiet. */
  .steps { margin-top: 7px; display: flex; flex-direction: column; gap: 3px; }
  .step {
    font: 11.5px/1.45 var(--mono);
    color: var(--dim);
    padding: 2px 8px;
    border-left: 2px solid var(--agent-soft);
  }
  .step.active { color: var(--agent); border-left-color: var(--agent); }

  /* Generic `monospace`, not the --mono chain: this is Claude's own code, which
     is answer content, and the chain is reserved for chrome labels. */
  code, pre { font-family: monospace; font-size: 12.5px; }
  pre { background: var(--bg); border: 1px solid var(--line); border-radius: 7px;
        padding: 8px 10px; overflow-x: auto; }
  a { color: var(--accent); }

  /* Nothing in this panel animates on a loop, but the two places a transition
     could arrive are the drop-out in pages.py and any future hover; honouring
     the preference is the contract, and here it is reachable -- WebKitGTK maps
     `prefers-reduced-motion` onto GTK's `gtk-enable-animations`, which
     perf.tune_gtk turns off whenever CB_LIGHT is on. That is the default, so
     on this machine this block is the live branch, not the exception. */
  @media (prefers-reduced-motion: reduce) {
    * { transition: none !important; animation: none !important;
        scroll-behavior: auto !important; }
  }
/*@hud@*/
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

  // ---- markdown ---------------------------------------------------------
  // Claude answers in markdown, so a body that prints its source shows the
  // asterisks and the fences instead of the formatting. This is a deliberately
  // small renderer -- headings, emphasis, code, lists, quotes, rules, links and
  // GFM tables -- because the alternative is shipping a parser into a browser
  // that has to work offline.
  //
  // Everything is HTML-escaped BEFORE any markup is produced, so model output
  // can never introduce an element. The only tags in the result are ones this
  // function wrote.
  var MD = (function () {
    function esc(s) {
      return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    function inline(s) {
      // Inline code is parked first: whatever is inside it must survive the
      // emphasis passes literally, backticks being markdown's own escape.
      var code = [];
      s = s.replace(/`([^`\n]+)`/g, function (_m, c) {
        code.push('<code>' + c + '</code>');
        return '\u0001' + (code.length - 1) + '\u0001';
      });
      s = s.replace(/\[([^\]\n]*)\]\(([^)\s]+)\)/g, function (_m, t, href) {
        // Only schemes that cannot execute, and quotes stripped so the href
        // cannot end the attribute early.
        // Anything that is not a plainly inert scheme is left as the source
        // markdown wrote it, rather than half-rewritten into bare text.
        if (!/^(https?:|mailto:)/i.test(href)) return _m;
        return '<a href="' + href.replace(/["']/g, '') + '">' + (t || href) + '</a>';
      });
      s = s.replace(/\*\*([^\n]+?)\*\*/g, '<strong>$1</strong>');
      s = s.replace(/(^|[\s(\[])\*([^*\n]+?)\*/g, '$1<em>$2</em>');
      s = s.replace(/(^|[\s(\[])_([^_\n]+?)_/g, '$1<em>$2</em>');
      s = s.replace(/~~([^\n]+?)~~/g, '<del>$1</del>');
      return s.replace(/\u0001(\d+)\u0001/g, function (_m, i) { return code[+i]; });
    }

    function cells(line) {
      return line.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|')
                 .map(function (c) { return c.trim(); });
    }

    function isRule(line) {
      // A table's --- separator, distinguished from a horizontal rule by the
      // pipes it must contain.
      return /\|/.test(line) && /^[\s:|-]*-[\s:|-]*$/.test(line);
    }

    return function (src) {
      var fences = [];
      // A fence still streaming has no closer yet; ending at $ renders the code
      // that has arrived instead of dumping the rest of the answer into a <pre>
      // only once the model gets around to closing it.
      var t = esc(String(src)).replace(/\r\n?/g, '\n')
        .replace(/```[^\n`]*\n?([\s\S]*?)(?:\n```|```|$)/g, function (_m, code) {
          fences.push('<pre><code>' + code + '</code></pre>');
          return '\u0000' + (fences.length - 1) + '\u0000';
        });

      var lines = t.split('\n'), i = 0, html = '';
      var para = [], list = null, quote = [];

      function flushPara() {
        if (para.length) { html += '<p>' + inline(para.join(' ')) + '</p>'; para = []; }
      }
      function flushList() {
        if (!list) return;
        html += '<' + list.tag + '>' + list.items.map(function (x) {
          return '<li>' + inline(x) + '</li>';
        }).join('') + '</' + list.tag + '>';
        list = null;
      }
      function flushQuote() {
        if (quote.length) {
          html += '<blockquote>' + inline(quote.join(' ')) + '</blockquote>';
          quote = [];
        }
      }
      function flushAll() { flushPara(); flushList(); flushQuote(); }

      while (i < lines.length) {
        var ln = lines[i], m;

        m = ln.match(/^\u0000(\d+)\u0000\s*$/);
        if (m) { flushAll(); html += fences[+m[1]]; i++; continue; }

        if (/^\s*$/.test(ln)) { flushAll(); i++; continue; }

        m = ln.match(/^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/);
        if (m) {
          flushAll();
          // Demoted two levels: the card already owns the h3 above the body, so
          // an answer's own "# Heading" must not outrank the card's title.
          var lvl = Math.min(m[1].length + 2, 6);
          html += '<h' + lvl + '>' + inline(m[2]) + '</h' + lvl + '>';
          i++; continue;
        }

        if (/^\s{0,3}([-*_])\s*(\1\s*){2,}$/.test(ln)) {
          flushAll(); html += '<hr>'; i++; continue;
        }

        if (/\|/.test(ln) && i + 1 < lines.length && isRule(lines[i + 1])) {
          flushAll();
          var head = cells(ln), rows = [];
          i += 2;
          while (i < lines.length && /\|/.test(lines[i]) && !/^\s*$/.test(lines[i])) {
            rows.push(cells(lines[i])); i++;
          }
          html += '<table><thead><tr>' + head.map(function (c) {
            return '<th>' + inline(c) + '</th>';
          }).join('') + '</tr></thead><tbody>' + rows.map(function (r) {
            return '<tr>' + r.map(function (c) {
              return '<td>' + inline(c) + '</td>';
            }).join('') + '</tr>';
          }).join('') + '</tbody></table>';
          continue;
        }

        // &gt;, not >: the whole text was escaped before this parser ran, so
        // a quote marker no longer looks like one.
        m = ln.match(/^\s{0,3}&gt;\s?(.*)$/);
        if (m) { flushPara(); flushList(); quote.push(m[1]); i++; continue; }

        var ul = ln.match(/^\s*[-*+]\s+(.*)$/);
        var ol = ln.match(/^\s*\d+[.)]\s+(.*)$/);
        if (ul || ol) {
          flushPara(); flushQuote();
          var tag = ul ? 'ul' : 'ol';
          if (!list || list.tag !== tag) { flushList(); list = { tag: tag, items: [] }; }
          list.items.push((ul || ol)[1]);
          i++; continue;
        }

        // An indented line under a list item is that item continuing.
        if (list && !para.length && /^\s+\S/.test(ln)) {
          list.items[list.items.length - 1] += ' ' + ln.trim();
          i++; continue;
        }

        flushList(); flushQuote();
        para.push(ln.trim());
        i++;
      }
      flushAll();
      return html;
    };
  })();
  window.__md = MD;   // so the renderer can be exercised without a model

  // Re-rendering the whole body per chunk is cheap (an answer is a page or two)
  // and is the only way to stay correct while streaming: markdown is not
  // parseable one fragment at a time. One render per frame, not per chunk.
  function paint(el) {
    if (el.__painting) return;
    el.__painting = true;
    requestAnimationFrame(function () {
      el.__painting = false;
      el.querySelector('.body').innerHTML = MD(el.__raw || '');
      scroll();
    });
  }

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
      // The markdown source is the record; the DOM is a view of it. Appending
      // to the DOM instead would mean parsing a chunk at a time, which cannot
      // work -- a fence or a list is not knowable from its first fragment.
      el.__raw = (el.__raw || '') + text;
      paint(el);
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
      // A final synchronous render: the last chunk's frame may not have run,
      // and an unclosed fence mid-stream settles here.
      el.__painting = false;
      el.querySelector('.body').innerHTML = MD(el.__raw || '');
      scroll();
    }
  };
})();
</script>
"""


# ---------------------------------------------------------------------------
# The panel's half of the HUD, appended for phosphor only -- the same shape as
# style.py's `_HUD` and for the same reason: dark and light keep exactly the
# panel they had, so picking phosphor is a visible choice rather than a silent
# redesign of the other two.
#
# The rules here are geometry and type, never colour. Every ink is already
# decided by the token contract and has already been held to its ratio; what
# changes is that corners go square, cards gain registration marks, and the
# labels above them read as engraved rather than typeset.
# ---------------------------------------------------------------------------
_HUD = """
  .card, .hint kbd, pre, .body code, .hs, .pw { border-radius: 0; }
  .card { border-color: %(edge)s; border-left-width: 2px; position: relative; }

  /* Registration marks: two per card, diagonally opposite. Four would read as
     a second border; two read as a mark on an instrument. Pseudo-elements, so
     nothing is added to the DOM the streaming path has to walk. */
  .card::before, .card::after {
    content: ""; position: absolute; width: 6px; height: 6px;
    border: 1px solid %(edge)s; pointer-events: none;
  }
  .card::before { top: -1px; right: -1px; border-left: none; border-bottom: none; }
  .card::after { bottom: -1px; right: -1px; border-left: none; border-top: none; }
  .card.ok::before, .card.ok::after { border-color: %(ok)s; }
  .card.error::before, .card.error::after { border-color: %(warn)s; }

  /* The card title is a legend, not a sentence: monospaced and tracked out.
     The body under it keeps the proportional face -- it is prose, and prose set
     in monospace is a costume, not a design. */
  .card h3 {
    font-family: %(mono)s;
    font-size: 10px;
    letter-spacing: .22em;
  }
  .meta { font-family: %(mono)s; letter-spacing: .06em; font-size: 11px; }
  .step { letter-spacing: .04em; }
  /* The running step gets the one glow in the panel, in the agent ink -- at a
     glance, colour and bloom together say the machine is waiting on Claude. */
  .step.active { box-shadow: -2px 0 8px -2px %(agent)s99; }

  .hint { font-family: %(mono)s; font-size: 12px; line-height: 1.7; }
  .hint kbd { border-bottom-width: 1px; border-color: %(edge)s; }
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
