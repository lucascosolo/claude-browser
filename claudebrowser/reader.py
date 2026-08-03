"""Reader mode: find the article on a page and re-render it for reading.

Kept free of any GTK import, like tabnames.py and urls.py, so the parts worth
testing -- option clamping, the stylesheet, and the shape of the injected
snippet -- can be exercised without a display.

Two decisions are worth spelling out.

*The page is not rewritten.* The extracted article is painted into a fixed
overlay stacked on top of the document, and toggling off removes the overlay
and nothing else. Stripping the live DOM would be a one-way trip: a single
page-app that re-renders after we deleted half its tree cannot be put back by
reloading without losing the user's scroll, their form state, or a load the
agent is waiting on.

*The scoring runs on the live tree, the cleaning on a clone.* Scoring wants
`querySelectorAll` over the real document; cleaning wants to delete freely.
Cloning first and scoring the copy would work too, but it pays for a full DOM
copy on pages we then decide have no article at all.

The heuristic is deliberately small: score the blocks that hold prose, credit
their parent and grandparent, discount for link density, and nudge on the
class and id names that a decade of CMS templates made near-universal. It is
Readability's idea at a tenth of the size, and it is wrong in the same places
Readability is wrong -- pages whose article is one <div> of <br>-separated
text score nothing and fall back to <article>/<main>/<body>.
"""

from . import extract

#: Reading speed for the minutes estimate. Adult prose reading is usually put
#: between 200 and 250 wpm; the exact number matters less than not recomputing
#: it in two languages -- the injected script counts words and stops there, and
#: this side turns the count into an estimate.
WPM = 220

DEFAULT_FONT_PX = 20
DEFAULT_WIDTH_PX = 700

#: Clamps, not validation errors. These arrive from a CLI flag or an MCP
#: argument, and a nonsensical 4000px measure should give the caller a readable
#: page rather than a refusal it has to learn to avoid.
FONT_RANGE = (12, 34)
WIDTH_RANGE = (360, 1100)


def options(font_px=None, width_px=None):
    """Normalize the two knobs into a dict the rest of the module can trust."""
    return {"font_px": _clamp(font_px, DEFAULT_FONT_PX, FONT_RANGE),
            "width_px": _clamp(width_px, DEFAULT_WIDTH_PX, WIDTH_RANGE)}


def minutes(words):
    """Reading time in whole minutes, never zero for an article that exists."""
    try:
        count = int(words)
    except (TypeError, ValueError):
        return 0
    if count <= 0:
        return 0
    return max(1, round(count / WPM))


def stylesheet(opts=None):
    """The reading typography, as one stylesheet scoped to the overlay.

    Everything is written against `#cb-reader-root` so the page's own rules
    lose on specificity, and the overlay carries its own colours rather than
    inheriting a site theme it is meant to be an escape from.
    """
    opts = opts or options()
    font = opts["font_px"]
    width = opts["width_px"]
    return _CSS.replace("$FONT", str(font)).replace("$WIDTH", str(width))


def toggle(font_px=None, width_px=None):
    """JavaScript that turns reader mode on if it is off, and off if it is on.

    Evaluates to a JSON string, like every snippet in extract.py.
    """
    css = stylesheet(options(font_px, width_px))
    return "(function(){var CSS=%s;%s})()" % (extract._js_str(css), _SCRIPT)


def _clamp(value, default, span):
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(span[0], min(span[1], number))


# The drop cap and the measure are the whole point of "read-paper": a fixed
# line length in the mid-70-character range is what makes a long page feel like
# a page rather than a wall, and no site's own layout is trying to give you one.
_CSS = """
#cb-reader-root{position:fixed;top:0;right:0;bottom:0;left:0;z-index:2147483647;
  overflow-y:auto;overflow-x:hidden;background:#faf8f3;color:#1e1c19;
  font-family:Georgia,"Iowan Old Style","Source Serif 4",Palatino,serif;
  font-size:$FONTpx;line-height:1.62;letter-spacing:.005em;
  padding:6vh 5vw 16vh;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;}
#cb-reader-root *{max-width:100%;box-sizing:border-box;}
#cb-reader-root .cb-reader-head,#cb-reader-root .cb-reader-body{
  max-width:$WIDTHpx;margin:0 auto;}
#cb-reader-root .cb-reader-head{border-bottom:1px solid rgba(0,0,0,.14);
  padding-bottom:1.1em;margin-bottom:2em;}
#cb-reader-root .cb-reader-head h1{font-size:2.1em;line-height:1.14;margin:0 0 .3em;
  font-weight:600;letter-spacing:-.012em;}
#cb-reader-root .cb-reader-meta{margin:0;font-size:.72em;line-height:1.5;
  letter-spacing:.09em;text-transform:uppercase;opacity:.55;
  font-family:system-ui,sans-serif;}
#cb-reader-root .cb-reader-body p{margin:0 0 1.15em;}
#cb-reader-root .cb-reader-body>p:first-of-type::first-letter{
  float:left;font-size:3.1em;line-height:.84;padding:.06em .09em 0 0;font-weight:600;}
#cb-reader-root .cb-reader-body h1,#cb-reader-root .cb-reader-body h2,
#cb-reader-root .cb-reader-body h3,#cb-reader-root .cb-reader-body h4{
  line-height:1.24;margin:1.9em 0 .55em;font-weight:600;letter-spacing:-.008em;}
#cb-reader-root .cb-reader-body h1{font-size:1.6em;}
#cb-reader-root .cb-reader-body h2{font-size:1.35em;}
#cb-reader-root .cb-reader-body h3{font-size:1.15em;}
#cb-reader-root .cb-reader-body h4{font-size:1em;letter-spacing:.04em;text-transform:uppercase;}
#cb-reader-root .cb-reader-body a{color:inherit;text-decoration:underline;
  text-decoration-color:rgba(190,110,70,.75);text-underline-offset:.16em;}
#cb-reader-root .cb-reader-body blockquote{margin:1.6em 0;padding:0 0 0 1.1em;
  border-left:3px solid rgba(190,110,70,.5);font-style:italic;opacity:.92;}
#cb-reader-root .cb-reader-body pre{background:rgba(0,0,0,.055);padding:.9em 1em;
  border-radius:6px;overflow-x:auto;font-size:.82em;line-height:1.5;
  font-family:ui-monospace,"DejaVu Sans Mono",monospace;font-style:normal;}
#cb-reader-root .cb-reader-body code{font-family:ui-monospace,"DejaVu Sans Mono",monospace;
  font-size:.86em;background:rgba(0,0,0,.055);padding:.08em .3em;border-radius:4px;}
#cb-reader-root .cb-reader-body pre code{background:none;padding:0;font-size:1em;}
#cb-reader-root .cb-reader-body ul,#cb-reader-root .cb-reader-body ol{
  margin:0 0 1.15em;padding-left:1.35em;}
#cb-reader-root .cb-reader-body li{margin:.32em 0;}
#cb-reader-root .cb-reader-body img{display:block;height:auto;margin:1.6em auto;border-radius:4px;}
#cb-reader-root .cb-reader-body figcaption{font-size:.78em;text-align:center;opacity:.6;
  margin-top:-1em;margin-bottom:1.6em;font-family:system-ui,sans-serif;}
#cb-reader-root .cb-reader-body hr{border:0;border-top:1px solid rgba(0,0,0,.15);margin:2.2em 0;}
#cb-reader-root .cb-reader-body table{border-collapse:collapse;font-size:.86em;margin:0 0 1.4em;}
#cb-reader-root .cb-reader-body td,#cb-reader-root .cb-reader-body th{
  border:1px solid rgba(0,0,0,.16);padding:.4em .6em;text-align:left;}
#cb-reader-root::-webkit-scrollbar{width:10px;}
#cb-reader-root::-webkit-scrollbar-thumb{background:rgba(0,0,0,.2);border-radius:6px;}
@media (prefers-color-scheme:dark){
  #cb-reader-root{background:#171614;color:#e6e1d8;}
  #cb-reader-root .cb-reader-head{border-bottom-color:rgba(255,255,255,.16);}
  #cb-reader-root .cb-reader-body pre,#cb-reader-root .cb-reader-body code{
    background:rgba(255,255,255,.07);}
  #cb-reader-root .cb-reader-body hr{border-top-color:rgba(255,255,255,.18);}
  #cb-reader-root .cb-reader-body td,#cb-reader-root .cb-reader-body th{
    border-color:rgba(255,255,255,.18);}
  #cb-reader-root::-webkit-scrollbar-thumb{background:rgba(255,255,255,.2);}
}
"""

# The body of the IIFE `toggle` builds. `CSS` is already in scope.
_SCRIPT = r"""
var ROOT_ID='cb-reader-root', STYLE_ID='cb-reader-style';
var live=document.getElementById(ROOT_ID);
if(live){
  live.remove();
  var old=document.getElementById(STYLE_ID); if(old)old.remove();
  document.documentElement.style.overflow=window.__cbReaderOverflow||'';
  window.__cbReaderOverflow=null;
  return JSON.stringify({ok:true,reader:false,url:location.href,title:document.title});
}

var POS=/(article|blog|body|content|entry|main|markdown|post|prose|story|text)/i;
var NEG=/(advert|banner|breadcrumb|comment|cookie|disqus|footer|header|masthead|menu|meta|modal|nav|newsletter|paywall|popup|promo|related|share|sidebar|social|sponsor|subscribe|toolbar|widget)/i;
var CHROME='nav,header,footer,aside,form,figure>figcaption,[role="navigation"],[role="banner"],[role="complementary"],[role="search"]';
var DROP='script,style,noscript,iframe,svg,canvas,form,button,input,textarea,select,object,video,audio,nav,header,footer,aside,link,meta,[aria-hidden="true"],[hidden],[role="navigation"],[role="banner"],[role="complementary"]';
var KEEP=/^(P|H1|H2|H3|H4|H5|H6|UL|OL|LI|PRE|CODE|KBD|SAMP|BLOCKQUOTE|FIGURE|FIGCAPTION|IMG|PICTURE|SOURCE|A|EM|STRONG|B|I|U|S|BR|HR|TABLE|THEAD|TBODY|TFOOT|TR|TD|TH|CAPTION|SPAN|DIV|SECTION|ARTICLE|MAIN|DL|DT|DD|SUP|SUB|MARK|SMALL|TIME|ABBR|CITE|Q|DETAILS|SUMMARY)$/;

function textOf(n){ return ((n&&n.textContent)||'').replace(/\s+/g,' ').trim(); }
function nameOf(n){
  // SVG elements carry an SVGAnimatedString here, not a string.
  var cls=(n&&typeof n.className==='string')?n.className:'';
  return cls+' '+((n&&n.id)||'');
}
function linkDensity(n){
  var total=textOf(n).length;
  if(!total) return 1;
  var linked=0;
  n.querySelectorAll('a[href]').forEach(function(a){ linked+=textOf(a).length; });
  return Math.min(1, linked/total);
}

// Prose blocks credit their container: the parent in full, the grandparent at
// half. A wrapper that holds the article wins on the sum without anyone having
// to guess which tag the site chose for it.
var scores=new Map();
function bump(node,amount){
  if(!node||node.nodeType!==1) return;
  if(node===document.body||node===document.documentElement) return;
  scores.set(node,(scores.get(node)||0)+amount);
}
document.querySelectorAll('p,pre,blockquote,li,h2,h3').forEach(function(el){
  if(el.closest(CHROME)) return;
  var text=textOf(el);
  if(text.length<25) return;
  var base=1+Math.min(Math.floor(text.length/100),3)
            +Math.min((text.match(/,/g)||[]).length,3);
  bump(el.parentNode,base);
  bump(el.parentNode&&el.parentNode.parentNode,base/2);
});

var best=null,bestScore=0;
scores.forEach(function(score,node){
  var hint=0, name=nameOf(node);
  if(NEG.test(name)) hint-=25;
  if(POS.test(name)) hint+=25;
  var final=(score+hint)*(1-linkDensity(node));
  if(final>bestScore){ bestScore=final; best=node; }
});
var article=best||document.querySelector('article,main,[role="main"]')||document.body;
if(!article) return JSON.stringify({ok:false,reader:false,error:'no document to read'});

var clone=article.cloneNode(true);
clone.querySelectorAll(DROP).forEach(function(n){ n.remove(); });
clone.querySelectorAll('*').forEach(function(n){
  if(!n.parentNode) return;                      // already gone with an ancestor
  if(!KEEP.test(n.tagName)){ n.remove(); return; }
  // A boilerplate name only condemns a short block. Long ones are sometimes
  // the article itself sitting in a div called "post-content-share-wrapper".
  if(NEG.test(nameOf(n))&&textOf(n).length<400&&!n.querySelector('p')){ n.remove(); return; }
  var allowed=n.tagName==='A'?['href']
             :(n.tagName==='IMG'||n.tagName==='SOURCE')?['src','srcset','alt']
             :[];
  var names=[];
  for(var i=0;i<n.attributes.length;i++) names.push(n.attributes[i].name);
  names.forEach(function(attr){
    if(allowed.indexOf(attr)<0) n.removeAttribute(attr);
  });
});

var heading=article.querySelector('h1')||document.querySelector('h1');
var title=(heading&&textOf(heading))||document.title||location.href;
// The overlay prints the title itself, so a leading copy of it in the body is
// the same line twice.
var first=clone.querySelector('h1');
if(first&&textOf(first)===title) first.remove();

var byline='';
var author=document.querySelector('meta[name="author"],meta[property="article:author"]');
if(author) byline=(author.getAttribute('content')||'').trim();
if(!byline){
  var node=document.querySelector('[rel~="author"],[itemprop="author"],.byline,.author');
  if(node) byline=textOf(node).slice(0,120);
}

var style=document.createElement('style');
style.id=STYLE_ID;
style.textContent=CSS;
(document.head||document.documentElement).appendChild(style);

var root=document.createElement('div');
root.id=ROOT_ID;
root.setAttribute('lang',document.documentElement.getAttribute('lang')||'');
var head=document.createElement('header');
head.className='cb-reader-head';
var h=document.createElement('h1');
h.textContent=title;
head.appendChild(h);

var body=document.createElement('article');
body.className='cb-reader-body';
while(clone.firstChild) body.appendChild(clone.firstChild);
var words=textOf(body).split(/\s+/).filter(function(w){ return w.length; }).length;

var meta=document.createElement('p');
meta.className='cb-reader-meta';
meta.textContent=[byline,location.hostname,words+' words'].filter(function(p){
  return p;
}).join('  ·  ');
head.appendChild(meta);

root.appendChild(head);
root.appendChild(body);
document.documentElement.appendChild(root);

// Too little survived to be worth reading: undo rather than leave the user
// staring at an empty sheet with no obvious way back.
if(words<40){
  root.remove(); style.remove();
  return JSON.stringify({ok:false,reader:false,url:location.href,
                         error:'no article content found on this page'});
}

window.__cbReaderOverflow=document.documentElement.style.overflow;
document.documentElement.style.overflow='hidden';
// Focusable, or Page Down keeps scrolling the document underneath.
root.tabIndex=-1;
root.focus();

return JSON.stringify({ok:true,reader:true,url:location.href,title:title,
                       byline:byline,words:words,blocks:body.children.length});
"""
