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

BLOCKED = r"""
(function () {
  var html = document.documentElement.innerHTML;
  var text = document.body ? document.body.innerText : '';
  var title = (document.title || '').toLowerCase();
  var checks = [
    ['recaptcha', /recaptcha/i.test(html) ||
                  !!document.querySelector('iframe[src*="recaptcha"], .g-recaptcha')],
    ['hcaptcha', /hcaptcha/i.test(html) ||
                 !!document.querySelector('iframe[src*="hcaptcha"], .h-captcha')],
    ['turnstile', !!document.querySelector('.cf-turnstile, #cf-challenge-stage') ||
                  title.indexOf('just a moment') >= 0],
    ['generic', /verify you.{0,3}(are|.re).{0,3}(a )?human|i.?m not a robot|unusual traffic|access denied/i.test(text)],
  ];
  for (var i = 0; i < checks.length; i++) {
    if (checks[i][1]) return JSON.stringify({ blocked: true, kind: checks[i][0] });
  }
  return JSON.stringify({ blocked: false, kind: null });
})()
"""


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


# Installed once per document via a document-start UserScript (see
# browser.py's add_script loop, alongside CONSOLE_SHIM). __cbEpoch is
# unconditional -- unlike __cbHalo's `if (window.__cbHalo) return` guard, a
# stale epoch surviving a same-document re-run is exactly the silent-wrong-
# click failure mode this exists to prevent: a snapshot taken before a
# navigation must never resolve against the document that replaced it.
#
# __cbRegistry maps a ref ("e7") to the node snapshot() found there, plus a
# signature (role/type/label) and a structural path -- both used by
# __cbResolve as a fallback when the node itself has been replaced (a
# framework re-render swaps DOM nodes but usually preserves their shape).
SNAPSHOT_SHIM = r"""
(function () {
  window.__cbEpoch = Math.random().toString(36).slice(2);
  window.__cbRegistry = window.__cbRegistry || {};
  window.__cbRegister = function (ref, node, sig, path) {
    window.__cbRegistry[ref] = { node: node, sig: sig, path: path };
  };
  window.__cbSig = function (node) {
    var role = node.getAttribute('role') || node.tagName.toLowerCase();
    var label = (node.getAttribute('aria-label')
                 || (node.labels && node.labels[0] && node.labels[0].innerText)
                 || node.getAttribute('placeholder')
                 || node.innerText || '').trim().slice(0, 80);
    return role + '|' + (node.type || '') + '|' + label;
  };
  window.__cbResolve = function (ref) {
    var entry = window.__cbRegistry[ref];
    if (!entry) return null;
    if (entry.node && entry.node.isConnected
        && window.__cbSig(entry.node) === entry.sig) return entry.node;
    // Structural fallback: re-walk the recorded frame/nth-child path.
    if (entry.path) {
      try {
        var node = document.querySelector(entry.path);
        if (node && window.__cbSig(node) === entry.sig) return node;
      } catch (e) {}
    }
    return null;
  };
})()
"""


def snapshot() -> str:
    """Interactive elements as a compact, ref-indexed line list, not prose.

    Same-origin iframes are walked (contentDocument is reachable from the
    top frame); cross-origin ones are reported as unreachable rather than
    silently yielding no matches -- an agent deciding a form cannot be
    filled needs to know *why*, not just that nothing came back.

    A <select>'s option COUNT is reported, never its option list: inlining
    every <option> is exactly the per-turn token cost this feature exists to
    avoid. An agent that needs the choices asks for them explicitly.
    """
    return r"""
(function () {
  var out = [];
  var n = 0;
  function label(el) {
    var l = el.getAttribute('aria-label')
      || (el.labels && el.labels[0] && el.labels[0].innerText)
      || el.getAttribute('placeholder')
      || (el.innerText || '').trim();
    return (l || '').trim().slice(0, 80);
  }
  function sig(el) {
    return (el.getAttribute('role') || el.tagName.toLowerCase())
      + '|' + (el.type || '') + '|' + label(el);
  }
  function walk(doc, path) {
    if (!doc) return;
    var els = doc.querySelectorAll(
      'input,textarea,select,button,a[href],[role=button],[contenteditable]');
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (!(el.getClientRects().length > 0)) continue;
      n++;
      var ref = 'e' + n;
      var elPath = path + ' ' + el.tagName.toLowerCase() + ':nth-child(' + (i + 1) + ')';
      if (typeof window.__cbRegister === 'function') {
        window.__cbRegister(ref, el, sig(el), elPath);
      }
      var row = { ref: ref, tag: el.tagName.toLowerCase(), label: label(el) };
      if (el.tagName === 'SELECT') row.options = el.options.length;
      if (el.tagName === 'A') row.href = el.href;
      if (el.required) row.required = true;
      out.push(row);
    }
    var frames = doc.querySelectorAll('iframe');
    for (var j = 0; j < frames.length; j++) {
      var f = frames[j];
      try {
        if (f.contentDocument) {
          walk(f.contentDocument, path + ' f' + j);
        } else {
          out.push({ ref: 'f' + j, tag: 'iframe',
                     origin: (new URL(f.src, location.href)).origin,
                     reason: 'UNREACHABLE' });
        }
      } catch (e) {
        out.push({ ref: 'f' + j, tag: 'iframe', reason: 'UNREACHABLE' });
      }
    }
  }
  walk(document, 'body');
  return JSON.stringify({
    ok: true, url: location.href,
    epoch: window.__cbEpoch || null,
    lines: out, counts: { total: out.length }
  });
})()
"""


def delta() -> str:
    """A fragment of object-literal properties appended to every acting op's
    return value: what actually changed, so the caller does not need a full
    re-read to find out. Assumes an enclosing scope has already captured
    `__cbUrlBefore` (the URL as it stood right before the action ran).

    `blocked` reuses the same signals as BLOCKED above, compacted -- a click
    that triggers a CAPTCHA is exactly the moment an agent needs to know
    without spending a whole extra round trip on `blocked` to find out.
    """
    return r"""
    url_changed: (location.href !== __cbUrlBefore),
    epoch: window.__cbEpoch || null,
    blocked: (function () {
      var html = document.documentElement.innerHTML;
      var title = (document.title || '').toLowerCase();
      return /recaptcha/i.test(html) || /hcaptcha/i.test(html) ||
             !!document.querySelector(
               'iframe[src*="recaptcha"], .g-recaptcha, ' +
               'iframe[src*="hcaptcha"], .h-captcha, ' +
               '.cf-turnstile, #cf-challenge-stage') ||
             title.indexOf('just a moment') >= 0;
    })(),
    changed: document.querySelectorAll(
      '[aria-invalid="true"],[role="alert"],.error,:invalid').length,
    errors: (function () {
      var els = document.querySelectorAll('[aria-invalid="true"],[role="alert"],.error');
      var msgs = [];
      for (var i = 0; i < els.length && i < 5; i++) {
        var t = (els[i].innerText || '').trim();
        if (t) msgs.push(t);
      }
      return msgs;
    })()
"""


def _resolve_target(selector: str) -> str:
    """A ref target ("@e7") resolves via __cbResolve (populated by a prior
    snapshot()); anything else is a plain querySelector -- unchanged
    behavior for every existing caller that only ever passes a CSS
    selector."""
    if selector.startswith("@"):
        return "(window.__cbResolve && window.__cbResolve(%s))" % _js_str(selector[1:])
    return "document.querySelector(%s)" % _js_str(selector)


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
    """Click the first match -- a plain CSS selector, or an "@ref" from a
    prior snapshot(). Reports whether anything was actually hit -- a silent
    no-op is the worst possible answer to give an agent -- plus a delta()
    of what the click changed."""
    return (
        HALO +
        "(function(){var __cbUrlBefore=location.href;var e=%s;"
        "if(!e)return JSON.stringify({ok:false,error:'no match'});"
        # Instant, not smooth: point() has usually centred this already, and
        # when it has not, the halo and the cursor must land on a settled rect
        # in the same turn -- there is no second measurement here.
        "e.scrollIntoView({block:'center'});"
        # After scrollIntoView, so the halo lands on where the element ended up.
        "window.__cbHaloAt(e);window.__cbCursorAt(e,true);e.click();"
        "return JSON.stringify(Object.assign("
        "{ok:true,tag:e.tagName.toLowerCase()},{%s}));})()"
        % (_resolve_target(selector), delta())
    )


def fill(selector: str, value: str) -> str:
    """Set a field's value and fire input+change, so frameworks that listen for
    events (React, Vue) actually see the write. Assigning .value alone does
    not. `selector` is a plain CSS selector or an "@ref" from a prior
    snapshot(); the result carries a delta() of what the write changed."""
    return (
        HALO +
        "(function(){var __cbUrlBefore=location.href;var e=%s;"
        "if(!e)return JSON.stringify({ok:false,error:'no match'});"
        "e.scrollIntoView({block:'center'});window.__cbHaloAt(e);"
        "window.__cbCursorAt(e,true);"
        "e.focus();e.value=%s;"
        "e.dispatchEvent(new Event('input',{bubbles:true}));"
        "e.dispatchEvent(new Event('change',{bubbles:true}));"
        "return JSON.stringify(Object.assign({ok:true},{%s}));})()"
        % (_resolve_target(selector), _js_str(value), delta())
    )


def fill_many(pairs) -> str:
    """Fill several fields in one round trip. `pairs` is a list of
    (selector_or_ref, value) tuples, already resolved against the profile
    vault by the caller -- this function only ever sees literal strings to
    type, never a {profile:...} placeholder (see fills.py). One unresolvable
    target does not abort the rest: each entry reports its own ok/error, so
    a caller can see exactly which fields need a different selector."""
    entries = ",".join(
        "[%s, %s]" % (
            ("'@' + " + _js_str(sel[1:])) if sel.startswith("@")
            else _js_str(sel),
            _js_str(val))
        for sel, val in pairs)
    return (
        HALO +
        r"""
(function () {
  var __cbUrlBefore = location.href;
  var pairs = [%s];
  var results = [];
  for (var i = 0; i < pairs.length; i++) {
    var target = pairs[i][0];
    var el = target.charAt(0) === '@'
      ? (window.__cbResolve && window.__cbResolve(target.slice(1)))
      : document.querySelector(target);
    if (!el) { results.push({ ok: false, target: target, error: 'no match' }); continue; }
    el.scrollIntoView({ block: 'center' });
    if (window.__cbHaloAt) window.__cbHaloAt(el);
    if (window.__cbCursorAt) window.__cbCursorAt(el, true);
    el.focus();
    el.value = pairs[i][1];
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    results.push({ ok: true, target: target });
  }
  return JSON.stringify(Object.assign({ ok: true, results: results }, {%s}));
})()
""" % (entries, delta())
    )


def find(pattern: str, selector=None) -> str:
    """Case-insensitive text search over the rendered page, with context.
    With a selector (CSS or "@ref") only that element's innerText is searched,
    and a selector that matches nothing answers "no match"."""
    if selector:
        source = ("var r=%s;if(!r)return JSON.stringify({ok:false,error:'no match'});"
                  "var t=r.innerText||'';" % _resolve_target(selector))
    else:
        source = "var t=document.body?document.body.innerText:'';"
    return (
        "(function(){var re=new RegExp(%s,'gi');" % _js_str(pattern) + source +
        "var m,out=[];"
        "while((m=re.exec(t))&&out.length<50){"
        "out.push(t.slice(Math.max(0,m.index-80),m.index+m[0].length+80).replace(/\\s+/g,' '));"
        "if(m.index===re.lastIndex)re.lastIndex++;}"
        "return JSON.stringify({count:out.length,matches:out});})()"
    )


_MAIN_ROOT = """var root = document.querySelector('main,article,[role="main"]') || document.body;"""


def _scoped_root(snippet: str, selector, anchor: str, replacement: str) -> str:
    assert anchor in snippet, "walker changed; scoped read needs updating"
    return snippet.replace(anchor, replacement % _resolve_target(selector), 1)


def _scoped(const: str, selector, anchor: str, replacement: str) -> str:
    if not selector:
        return const
    return _scoped_root(const, selector, anchor, replacement)


_NO_MATCH = "if (!root) return JSON.stringify({ok:false,error:'no match'});"


def text(selector=None) -> str:
    """TEXT rooted at the match; with no selector, TEXT itself, byte for byte."""
    return _scoped(TEXT, selector, _MAIN_ROOT, "var root = %s; " + _NO_MATCH)


def markdown(selector=None) -> str:
    """MARKDOWN rooted at the match; with no selector, MARKDOWN itself."""
    return _scoped(MARKDOWN, selector, _MAIN_ROOT, "var root = %s; " + _NO_MATCH)


def links(selector=None) -> str:
    """LINKS limited to anchors under the match; with no selector, LINKS."""
    if not selector:
        return LINKS
    anchor = "var seen = {}, out = [];"
    assert anchor in LINKS and "document.querySelectorAll('a[href]')" in LINKS
    scoped = LINKS.replace(
        anchor,
        "var root = %s; " % _resolve_target(selector) + _NO_MATCH + " " + anchor, 1)
    return scoped.replace("document.querySelectorAll('a[href]')",
                          "root.querySelectorAll('a[href]')", 1)


def html(selector=None) -> str:
    """The match's outer HTML; with no selector, HTML itself."""
    if not selector:
        return HTML
    return ("(function(){var e=%s;"
            "if(!e)return JSON.stringify({ok:false,error:'no match'});"
            "return JSON.stringify({url:location.href,html:e.outerHTML});})()"
            % _resolve_target(selector))


def wait_predicate(selector=None, text=None, url=None, gone=False) -> str:
    """One expression answering {"matched": "selector"|"text"|"url"|null}: the
    first condition that holds right now. Polled by Browser.api_wait_for.
    `selector` means present and visible, or -- with gone -- not visible.
    `text` and `url` are case-insensitive regexes; a bad regex never matches."""
    if not (selector or text or url):
        raise ValueError("wait-for needs at least one of selector, text, url")
    parts = ["var m=null,gone=%s;" % ("true" if gone else "false")]
    branches = []
    if selector:
        branches.append(
            "if(function(){var e=%s;var v=!!(e&&e.getClientRects&&e.getClientRects().length>0);"
            "return gone?!v:v;}())m='selector';" % _resolve_target(selector))
    if text:
        branches.append(
            "if(new RegExp(%s,'i').test(document.body?document.body.innerText:''))m='text';"
            % _js_str(text))
    if url:
        branches.append("if(new RegExp(%s,'i').test(location.href))m='url';" % _js_str(url))
    parts.append("try{" + "else ".join(branches) + "}catch(x){}")
    parts.append("return JSON.stringify({matched:m});")
    return "(function(){" + "".join(parts) + "})()"


def scroll_js(to=None, by=None) -> str:
    """Scroll the window to "top", "bottom", or an element (CSS or "@ref"), or
    by a signed pixel count. Answers {ok, x, y, height, viewport, at_bottom}.
    An element target is centred instantly (so the numbers describe where it
    ended up, as in click) and the halo cursor is sent to it."""
    if (to in (None, "")) == (by is None):
        raise ValueError("scroll needs exactly one of to, by")
    if by is not None:
        act = "window.scrollBy({top:%d,left:0,behavior:'instant'});" % int(by)
    elif to == "top":
        act = "window.scrollTo({top:0,behavior:'instant'});"
    elif to == "bottom":
        act = ("window.scrollTo({top:document.documentElement.scrollHeight,"
               "behavior:'instant'});")
    else:
        act = ("var e=%s;if(!e)return JSON.stringify({ok:false,error:'no match'});"
               "e.scrollIntoView({block:'center',inline:'center'});"
               "window.__cbCursorAt(e,false);" % _resolve_target(to))
    # HALO is a statement; the comma form keeps the whole snippet one expression.
    return (
        "(" + HALO.strip().rstrip(";") + ",(function(){" + act +
        "var d=document.documentElement,y=Math.round(window.scrollY),"
        "vp=window.innerHeight,h=d.scrollHeight;"
        "return JSON.stringify({ok:true,x:Math.round(window.scrollX),y:y,height:h,"
        "viewport:vp,at_bottom:(y+vp>=h-2)});})())"
    )


def tables(selector=None, limit=200) -> str:
    """Every <table> (or those under `selector`) as {caption, headers, rows}.
    Headers come from <thead>, else from a first row made only of <th>. Cell
    text is innerText with whitespace collapsed. rowspan and colspan are NOT
    expanded: a spanning cell appears once, in the row that declares it, so
    later rows can be shorter than the header. `limit` caps rows per table."""
    limit = int(limit)
    if selector:
        scope = ("var r=%s;if(!r)return JSON.stringify({ok:false,error:'no match'});"
                 "var ts=r.tagName==='TABLE'?[r]:r.querySelectorAll('table');"
                 % _resolve_target(selector))
    else:
        scope = "var ts=document.querySelectorAll('table');"
    return (
        "(function(){" + scope +
        "var LIMIT=%d;" % limit +
        "function cell(c){return (c.innerText||c.textContent||'').replace(/\\s+/g,' ').trim();}"
        "function row(tr){return Array.prototype.map.call(tr.cells,cell);}"
        "var out=[];Array.prototype.forEach.call(ts,function(t){"
        "var rs=Array.prototype.slice.call(t.rows),headers=[];"
        "var hr=t.tHead&&t.tHead.rows.length?t.tHead.rows[0]:null;"
        "if(!hr&&rs.length&&Array.prototype.every.call(rs[0].cells,function(c){return c.tagName==='TH';})"
        "&&rs[0].cells.length)hr=rs[0];"
        "if(hr){headers=row(hr);rs=rs.filter(function(r){return r!==hr&&"
        "!(t.tHead&&t.tHead.contains(r));});}"
        "var cap=t.caption?cell(t.caption):'';"
        "out.push({caption:cap,headers:headers,rows:rs.slice(0,LIMIT).map(row)});});"
        "return JSON.stringify({ok:true,tables:out});})()"
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
