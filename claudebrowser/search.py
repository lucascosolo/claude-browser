"""Web search: the request, the response, and nothing about how it is drawn.

GTK-free like `watchlater.py`, and for the same reasons -- the part worth
testing is which bytes become which results, and that runs in the suite with no
display and no network.

The browser's omnibox has always handed anything that is not an address to a
search engine as a URL. `cb:search` replaces that with a page this browser
renders itself, which means it needs a search API rather than a search *site*.
LangSearch is the one configured here; the shape below is deliberately the
minimum any of them share, so a second provider is a table entry rather than a
rewrite.

Four decisions, each of which encodes something real.

*The key is a secret, so it lives where the other secret lives.* `CB_SEARCH_KEY`
joins `ANTHROPIC_API_KEY` and `CB_VPN_PROXY` in `envfile.SECRET_KEYS`: read
straight out of the settings file by the code that needs it, never exported to
`os.environ`, never handed to a child process, and not writable from inside the
browser. A settings page that could write it would be a route from a page the
agent is reading to the user's credential.

*Search goes through the VPN tunnel like everything else.* Same `vpn.api_route`
the Claude client and the queue fetch use. A query is the single most revealing
thing this browser sends anywhere -- it is a list of what someone wanted to know
-- so a direct socket here while the mode claims to be on would be the worst of
the three leaks, not the least.

*A failure is a result, not an exception.* Every error path returns a dict the
page can render. A browser whose search box raises is a browser with no search
box.

*Nothing is cached.* Results are a live query, like everything else the user can
change -- see the note on `envfile.values()`. A cached result set is a page that
disagrees with the web and cannot say why.
"""

import http.client
import json
import urllib.parse

from . import envfile, vpn

#: The credential. In `envfile.SECRET_KEYS`, so it is never exported and never
#: writable from the browser -- see the module note.
KEY_ENV = "CB_SEARCH_KEY"

#: How many results to ask for. Ten is LangSearch's maximum and its default;
#: asking for fewer would not make the page faster, because the cost is the
#: round trip rather than the rows.
COUNT = 10

TIMEOUT = 20

#: One provider today. A dict rather than four module-level constants so the
#: second one is an entry here instead of an `if` at every call site.
PROVIDERS = {
    "langsearch": {
        "host": "api.langsearch.com",
        "path": "/v1/web-search",
        # LangSearch nests its results two deep and calls the list `value`.
        # Named here rather than reached for inline so a provider that spells
        # it differently is a table change.
        "results_at": ("data", "webPages", "value"),
        "fields": {"title": "name", "url": "url", "snippet": "snippet",
                   "summary": "summary", "date": "datePublished"},
    },
}

DEFAULT_PROVIDER = "langsearch"


def api_key(path=None):
    """The configured key, or "" -- read fresh, never cached, never exported."""
    return (envfile.setting(KEY_ENV, "", path=path) or "").strip()


def configured(path=None):
    return bool(api_key(path))


class Result:
    """One hit, in the only shape the page knows about."""

    __slots__ = ("title", "url", "snippet", "summary", "date")

    def __init__(self, title="", url="", snippet="", summary="", date=""):
        self.title = title
        self.url = url
        self.snippet = snippet
        self.summary = summary
        self.date = date

    def as_dict(self):
        return {"title": self.title, "url": self.url, "snippet": self.snippet,
                "summary": self.summary, "date": self.date}

    def __repr__(self):
        return "<Result %r %s>" % (self.title[:40], self.url[:40])


def _dig(data, path):
    """Follow a tuple of keys, returning None rather than raising."""
    node = data
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def parse(payload, provider=DEFAULT_PROVIDER):
    """A provider's JSON -> `{"ok", "results", "error"}`.

    Never raises. The response comes off the network from a service that can
    change shape, and a search box that throws is worse than one that says it
    could not read the answer.
    """
    spec = PROVIDERS.get(provider)
    if spec is None:
        return {"ok": False, "error": "unknown search provider %r" % provider,
                "results": []}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "the search API did not return an object",
                "results": []}
    # LangSearch reports its own errors in a `code` alongside HTTP 200.
    code = payload.get("code")
    if isinstance(code, int) and code != 200:
        return {"ok": False, "results": [],
                "error": "search API said %s%s" % (
                    code, ": %s" % payload["msg"] if payload.get("msg") else "")}
    rows = _dig(payload, spec["results_at"])
    if not isinstance(rows, list):
        return {"ok": False, "results": [],
                "error": "no results in the response -- the API changed shape"}
    fields = spec["fields"]
    results = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = row.get(fields["url"]) or ""
        if not url:
            continue  # a result with nowhere to go is not a result
        results.append(Result(
            title=row.get(fields["title"]) or url,
            url=url,
            snippet=row.get(fields["snippet"]) or "",
            summary=row.get(fields["summary"]) or "",
            date=row.get(fields["date"]) or ""))
    return {"ok": True, "results": results, "error": ""}


def fetch(query, key, provider=DEFAULT_PROVIDER, count=COUNT, route=None,
          timeout=TIMEOUT, opener=None):
    """POST the query and return `(payload, error)`.

    Routed through the VPN tunnel exactly as `ai.py` and `watchlater.py` are.
    A search query is the most revealing thing this browser sends anywhere, so
    this is the last place that should be allowed to bypass the mode.
    """
    spec = PROVIDERS.get(provider)
    if spec is None:
        return None, "unknown search provider %r" % provider
    if not key:
        return None, "no search API key configured"
    if not (query or "").strip():
        return None, "nothing to search for"
    proxy, refusal = vpn.api_route() if route is None else route
    if refusal:
        return None, refusal

    body = json.dumps({"query": query, "count": count, "summary": True})
    headers = {"Authorization": "Bearer %s" % key,
               "Content-Type": "application/json",
               "Accept": "application/json",
               "Host": spec["host"],
               # One socket, used once: this is a single request per search and
               # holding a tunnel open through the proxy afterwards buys
               # nothing.
               "Connection": "close"}
    try:
        if proxy is not None:
            conn = (opener or vpn.open_tunnel)(proxy, spec["host"], 443, timeout)
        else:
            conn = http.client.HTTPSConnection(spec["host"], 443,
                                               timeout=timeout)
        try:
            conn.request("POST", spec["path"], body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            if response.status != 200:
                # 401 is the interesting one and gets said plainly: a wrong key
                # is a different fix from an unreachable service.
                return None, ("search API returned HTTP %s%s" % (
                    response.status,
                    " (the key was rejected)" if response.status in (401, 403)
                    else ""))
            try:
                return json.loads(raw.decode("utf-8", "replace")), ""
            except json.JSONDecodeError as e:
                return None, "the search API did not return JSON: %s" % e
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except (OSError, http.client.HTTPException, vpn.ProbeError) as e:
        return None, "could not reach the search API: %s" % e


def search(query, key=None, provider=DEFAULT_PROVIDER, count=COUNT, route=None,
           fetcher=None, path=None):
    """The one function the browser calls: fetch, then parse."""
    if key is None:
        key = api_key(path)
    payload, error = (fetcher or fetch)(query, key, provider, count, route)
    if error:
        return {"ok": False, "error": error, "results": []}
    return parse(payload, provider)


#: What the omnibox writes for a search. `urls.SEARCH` pastes a quoted query
#: into `%s`, so this is a template of exactly the same shape as the site URL
#: it replaces -- which is what lets `cb:search` become the default engine
#: without `urls.py` learning anything new.
TEMPLATE = "cb:search?q=%s"


def query_of(raw):
    """Pull `q` out of a `cb:search` query string, percent-decoded."""
    for part in (raw or "").split("&"):
        key, _sep, value = part.partition("=")
        if key == "q":
            return urllib.parse.unquote_plus(value)
    return ""
