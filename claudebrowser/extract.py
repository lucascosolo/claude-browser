"""JavaScript run inside the page to give an agent something worth reading.

Each constant is a self-contained expression that evaluates to a JSON string.
They are injected with evaluate_javascript(), so the last expression is the
return value -- every snippet ends in a JSON.stringify(...) call.
"""

# Readable text: the single most-used endpoint, so it gets the careful version.
TEXT = r"""
(function () {
  // Walks the LIVE dom rather than cloning it.
  //
  // The obvious implementation -- clone the subtree, delete the chrome, read
  // .innerText -- is both slower and subtly wrong. innerText is defined to do
  // layout, but a cloned node is detached and has no layout, so it silently
  // degrades to textContent semantics: you pay for a full DOM copy and still
  // get hidden text back with no block separation. Walking in place costs no
  // copy and lets us use getClientRects() as a real visibility test.
  var SKIP = /^(SCRIPT|STYLE|NOSCRIPT|SVG|CANVAS|IFRAME|NAV|HEADER|FOOTER|ASIDE|TEMPLATE|SELECT)$/;
  var BREAK = /^(P|DIV|SECTION|ARTICLE|LI|TR|BLOCKQUOTE|PRE|BR|H[1-6]|UL|OL|TABLE)$/;
  var root = document.querySelector('main,article,[role="main"]') || document.body;
  if (!root) return JSON.stringify({ title: document.title, url: location.href, text: '' });

  var out = [];
  (function walk(node) {
    for (var c = node.firstChild; c; c = c.nextSibling) {
      if (c.nodeType === 3) { out.push(c.nodeValue); continue; }
      if (c.nodeType !== 1) continue;
      if (SKIP.test(c.tagName)) continue;
      if (c.getAttribute('aria-hidden') === 'true' || c.hasAttribute('hidden')) continue;
      var role = c.getAttribute('role');
      if (role === 'navigation' || role === 'banner') continue;
      if (c.getClientRects().length === 0) continue;   // display:none / not rendered
      walk(c);
      if (BREAK.test(c.tagName)) out.push('\n');
    }
  })(root);

  var text = out.join('').replace(/[ \t ]+/g, ' ')
                         .replace(/ ?\n ?/g, '\n')
                         .replace(/\n{3,}/g, '\n\n')
                         .trim();
  return JSON.stringify({ title: document.title, url: location.href, text: text });
})()
"""

# Rough Markdown. Not a full converter -- headings, links, list items, code.
# Enough for an agent to understand structure without shipping a library.
MARKDOWN = r"""
(function () {
  var drop = 'script,style,noscript,svg,canvas,iframe,nav,header,footer,aside,[aria-hidden="true"]';
  var root = document.querySelector('main,article,[role="main"]') || document.body;
  var clone = root.cloneNode(true);
  clone.querySelectorAll(drop).forEach(function (n) { n.remove(); });
  var out = [];
  function walk(node) {
    if (node.nodeType === 3) { out.push(node.textContent.replace(/\s+/g, ' ')); return; }
    if (node.nodeType !== 1) return;
    var tag = node.tagName.toLowerCase();
    if (/^h[1-6]$/.test(tag)) {
      out.push('\n\n' + '#'.repeat(+tag[1]) + ' ' + node.innerText.trim() + '\n');
      return;
    }
    if (tag === 'a' && node.href) {
      var label = node.innerText.trim();
      if (label) out.push('[' + label + '](' + node.href + ')');
      return;
    }
    if (tag === 'pre') { out.push('\n\n```\n' + node.innerText.trim() + '\n```\n'); return; }
    if (tag === 'code') { out.push('`' + node.innerText.trim() + '`'); return; }
    if (tag === 'li') { out.push('\n- '); }
    if (tag === 'br') { out.push('\n'); }
    if (/^(p|div|section|tr|ul|ol|blockquote)$/.test(tag)) out.push('\n\n');
    for (var i = 0; i < node.childNodes.length; i++) walk(node.childNodes[i]);
  }
  walk(clone);
  var md = out.join('').replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim();
  return JSON.stringify({ title: document.title, url: location.href, markdown: md });
})()
"""

# Every link, deduped by href, label-first. Absolute URLs -- a relative href is
# useless to an agent that is deciding where to navigate next.
LINKS = r"""
(function () {
  var seen = {}, out = [];
  document.querySelectorAll('a[href]').forEach(function (a) {
    var href = a.href;
    if (!href || href.indexOf('javascript:') === 0 || seen[href]) return;
    seen[href] = 1;
    out.push({ text: (a.innerText || a.getAttribute('aria-label') || '').trim().slice(0, 200),
               href: href });
  });
  return JSON.stringify({ url: location.href, count: out.length, links: out });
})()
"""

HTML = r"""JSON.stringify({url: location.href, html: document.documentElement.outerHTML})"""

TITLE = r"""JSON.stringify({url: location.href, title: document.title})"""


# How long the page-side choreography takes, in milliseconds. agent.py reads
# these to pace its own steps, so the native side and the page agree on timing
# instead of each guessing at the other's.
SCROLL_SETTLE_MS = 260   # smooth scrollIntoView -> element at rest
TRAVEL_MS = 320          # cursor transition from wherever it was to the target
PRESS_MS = 140           # the click "press" dip, before it springs back


# A visible pulse wherever the agent just acted.
#
# Claude driving a page you are watching is unnerving when the page moves on its
# own and nothing says why. The halo is the cheapest honest answer: it marks the
# exact point of every synthetic click and every field write, so an action you
# did not make is attributable at a glance.
#
# Deliberately self-contained and idempotent -- it is prepended to snippets that
# run many times in one page's life, and it must never depend on a stylesheet,
# a font, or anything the page could have removed.
#
# It also carries the persistent cursor: the halo says "something happened
# here", the cursor says "this is where Claude is about to act". Both live in
# one snippet because every acting script needs both, and one guarded IIFE is
# cheaper to prepend than two.
_HALO_SRC = r"""
(function () {
  if (window.__cbHalo) return;
  window.__cbHalo = function (x, y) {
    try {
      var dot = document.createElement('div');
      dot.setAttribute('data-cb-halo', '1');
      dot.style.cssText = [
        'position:fixed', 'left:0', 'top:0', 'width:46px', 'height:46px',
        'margin:-23px 0 0 -23px', 'border-radius:50%', 'pointer-events:none',
        'z-index:2147483647', 'border:2px solid rgba(217,119,87,.9)',
        'background:radial-gradient(circle,rgba(217,119,87,.45) 0%,' +
          'rgba(217,119,87,.18) 45%,rgba(217,119,87,0) 70%)',
        'box-shadow:0 0 20px 6px rgba(217,119,87,.55)',
        'transform:translate(' + x + 'px,' + y + 'px) scale(.35)',
        'opacity:1',
        'transition:transform .5s cubic-bezier(.2,.8,.3,1),opacity .5s ease-out'
      ].join(';');
      (document.body || document.documentElement).appendChild(dot);
      requestAnimationFrame(function () {
        dot.style.transform = 'translate(' + x + 'px,' + y + 'px) scale(1.4)';
        dot.style.opacity = '0';
      });
      setTimeout(function () { if (dot.parentNode) dot.parentNode.removeChild(dot); }, 800);
    } catch (e) {}
  };
  window.__cbHaloAt = function (el) {
    if (!el || !el.getBoundingClientRect) return;
    var r = el.getBoundingClientRect();
    window.__cbHalo(r.left + r.width / 2, r.top + r.height / 2);
  };

  // The cursor itself. One element for the page's whole life, looked up by
  // attribute rather than kept in a variable: a same-document navigation or a
  // framework that rewrites document.body throws the node away while this
  // closure survives, and stashing a detached node would leave an invisible
  // cursor with no way to notice.
  window.__cbCursorEl = function () {
    var el = document.querySelector('[data-cb-cursor]');
    if (el && el.parentNode) return el;
    el = document.createElement('div');
    el.setAttribute('data-cb-cursor', '1');
    // aria-hidden keeps it out of extract.TEXT and MARKDOWN, both of which
    // drop hidden subtrees -- the cursor must never read back as page content.
    el.setAttribute('aria-hidden', 'true');
    el.style.cssText = [
      'position:fixed', 'left:0', 'top:0', 'width:22px', 'height:22px',
      'margin:-11px 0 0 -11px', 'border-radius:50%',
      'pointer-events:none',              // never steals a real user's click
      'z-index:2147483647', 'opacity:0',
      'border:2px solid rgba(255,255,255,.85)',
      'background:radial-gradient(circle,rgba(217,119,87,1) 0%,' +
        'rgba(217,119,87,.85) 55%,rgba(217,119,87,.25) 100%)',
      'box-shadow:0 0 14px 5px rgba(217,119,87,.75),0 1px 3px rgba(0,0,0,.4)',
      'transform:translate(0px,0px) scale(1)',
      // The travel itself: the point of the cursor is that you can follow it,
      // so it eases to the target rather than appearing on it.
      'transition:transform __TRAVEL__ms cubic-bezier(.3,.7,.2,1),opacity .2s ease-out'
    ].join(';');
    (document.body || document.documentElement).appendChild(el);
    return el;
  };

  // press=true dips the cursor and lets it spring back, so a click reads as a
  // click and not as the cursor merely arriving.
  window.__cbCursorTo = function (x, y, press) {
    try {
      var el = window.__cbCursorEl();
      var to = function (s) { el.style.transform =
        'translate(' + x + 'px,' + y + 'px) scale(' + s + ')'; };
      el.style.opacity = '1';
      to(press ? 0.55 : 1);
      if (press) setTimeout(function () { try { to(1); } catch (e) {} }, __PRESS__);
    } catch (e) {}
  };
  window.__cbCursorAt = function (el, press) {
    if (!el || !el.getBoundingClientRect) return;
    var r = el.getBoundingClientRect();
    window.__cbCursorTo(r.left + r.width / 2, r.top + r.height / 2, press);
  };

  // Centred and smooth, with a fallback: scrollIntoView's options-object form
  // is ignored by anything that only implements the boolean argument.
  window.__cbScrollTo = function (el) {
    try { el.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'center' }); }
    catch (e) { try { el.scrollIntoView(); } catch (e2) {} }
  };
})();
"""

HALO = (_HALO_SRC.replace("__TRAVEL__", str(TRAVEL_MS))
                 .replace("__PRESS__", str(PRESS_MS)))

def point(selector: str) -> str:
    """Scroll the match into view and send the cursor to it, without acting.

    Run before click/fill so the user sees where the click is going to land
    before it lands. Cheap to run on its own: it changes nothing about the page
    except the scroll position.
    """
    return (
        HALO +
        "(function(){var e=document.querySelector(%s);"
        "if(!e)return JSON.stringify({ok:false,error:'no match'});"
        "window.__cbScrollTo(e);"
        # The rect is read from the timeout, not now. A smooth scroll is still
        # animating when this function returns, so measuring here would aim the
        # cursor at where the element *was* -- the same ordering trap the halo
        # hits, one animation later.
        "setTimeout(function(){window.__cbCursorAt(e,false);},%d);"
        "return JSON.stringify({ok:true,tag:e.tagName.toLowerCase()});})()"
        % (_js_str(selector), SCROLL_SETTLE_MS)
    )


def click(selector: str) -> str:
    """Click the first match. Reports whether anything was actually hit --
    a silent no-op is the worst possible answer to give an agent."""
    return (
        HALO +
        "(function(){var e=document.querySelector(%s);"
        "if(!e)return JSON.stringify({ok:false,error:'no match'});"
        # Instant, not smooth: point() has usually centred this already, and
        # when it has not, the halo and the cursor must land on a settled rect
        # in the same turn -- there is no second measurement here.
        "e.scrollIntoView({block:'center'});"
        # After scrollIntoView, so the halo lands on where the element ended up.
        "window.__cbHaloAt(e);window.__cbCursorAt(e,true);e.click();"
        "return JSON.stringify({ok:true,tag:e.tagName.toLowerCase()});})()"
        % _js_str(selector)
    )


def fill(selector: str, value: str) -> str:
    """Set a field's value and fire input+change, so frameworks that listen for
    events (React, Vue) actually see the write. Assigning .value alone does not."""
    return (
        HALO +
        "(function(){var e=document.querySelector(%s);"
        "if(!e)return JSON.stringify({ok:false,error:'no match'});"
        "e.scrollIntoView({block:'center'});window.__cbHaloAt(e);"
        "window.__cbCursorAt(e,true);"
        "e.focus();e.value=%s;"
        "e.dispatchEvent(new Event('input',{bubbles:true}));"
        "e.dispatchEvent(new Event('change',{bubbles:true}));"
        "return JSON.stringify({ok:true});})()" % (_js_str(selector), _js_str(value))
    )


def find(pattern: str) -> str:
    """Case-insensitive text search over the rendered page, with context."""
    return (
        "(function(){var re=new RegExp(%s,'gi');"
        "var t=document.body?document.body.innerText:'';var m,out=[];"
        "while((m=re.exec(t))&&out.length<50){"
        "out.push(t.slice(Math.max(0,m.index-80),m.index+m[0].length+80).replace(/\\s+/g,' '));"
        "if(m.index===re.lastIndex)re.lastIndex++;}"
        "return JSON.stringify({count:out.length,matches:out});})()" % _js_str(pattern)
    )


def _js_str(s: str) -> str:
    """Render `s` as a JS string literal that is safe in any injection context.

    json.dumps gets most of the way there -- JSON string syntax is a subset of
    JS -- but two classes of character are legal raw inside JSON and still
    dangerous in JS:

      U+2028 / U+2029   legal in JSON, terminate a line in JS
      < and >           legal in JSON, but a literal "</script>" inside the
                        text closes an enclosing <script> block

    Every caller today hands the result to evaluate_javascript(), where the
    <script> case cannot bite. Escaping it anyway keeps the helper correct if a
    snippet is ever inlined into a page -- a caller should not have to know
    which context it is in to use this safely.
    """
    import json

    return (
        json.dumps(s)
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
