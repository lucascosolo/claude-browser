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


_TEMPLATE = r"""<!doctype html>
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

  /* The body is rendered markdown, so block spacing is the elements' own; a
     pre-wrap body would double every gap. Only <pre> keeps literal whitespace. */
  .body { word-wrap: break-word; overflow-wrap: anywhere; }
  .body:empty::after { content: "…"; color: var(--dim); }
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
