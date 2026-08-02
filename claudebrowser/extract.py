"""JavaScript run inside the page to give an agent something worth reading.

Each constant is a self-contained expression that evaluates to a JSON string.
They are injected with evaluate_javascript(), so the last expression is the
return value -- every snippet ends in a JSON.stringify(...) call.
"""

# Readable text. Strips the furniture (nav/script/style/footer) that makes a
# raw innerText dump mostly noise, then collapses whitespace runs. This is the
# single most-used endpoint, so it is worth the extra DOM walk.
TEXT = r"""
(function () {
  var drop = 'script,style,noscript,svg,canvas,iframe,nav,header,footer,aside,' +
             '[aria-hidden="true"],[role="navigation"],[role="banner"]';
  var root = document.querySelector('main,article,[role="main"]') || document.body;
  if (!root) return JSON.stringify({ title: document.title, url: location.href, text: '' });
  var clone = root.cloneNode(true);
  clone.querySelectorAll(drop).forEach(function (n) { n.remove(); });
  var text = (clone.innerText || '').replace(/[ \t\u00a0]+/g, ' ')
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


def click(selector: str) -> str:
    """Click the first match. Reports whether anything was actually hit --
    a silent no-op is the worst possible answer to give an agent."""
    return (
        "(function(){var e=document.querySelector(%s);"
        "if(!e)return JSON.stringify({ok:false,error:'no match'});"
        "e.scrollIntoView({block:'center'});e.click();"
        "return JSON.stringify({ok:true,tag:e.tagName.toLowerCase()});})()"
        % _js_str(selector)
    )


def fill(selector: str, value: str) -> str:
    """Set a field's value and fire input+change, so frameworks that listen for
    events (React, Vue) actually see the write. Assigning .value alone does not."""
    return (
        "(function(){var e=document.querySelector(%s);"
        "if(!e)return JSON.stringify({ok:false,error:'no match'});"
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
