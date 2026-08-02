# Ledger — dedup/simplify + make Claude Browser the default

Baseline on branch `cb-default` off 33553f0: **131 tests pass**, 7131 lines.

## Findings that shaped the plan

1. **The API surface is described four times** and has already drifted:
   - `control.ROUTES` + 19 `r_*` functions
   - `cbctl` argparse subcommands + an if/elif dispatch chain
   - `cb-mcp` `TOOLS` table — **missing `forward`, `wait`, `health`**
   - `agent.TOOLS` (tuned for the model; deliberately left alone)
2. **`cbctl` and `cb-mcp` duplicate their whole transport**: sys.path bootstrap,
   envfile load, `BASE`/`TOKEN`, and a `call()` that differs only in error shape.
3. **`Browser.call_sync` and `Control._call` are the same GTK-loop bridge**, written twice.
4. **A second launch dies instead of opening the URL.** Verified: with an instance
   running, `./cb https://example.com` exits 1 with "cannot bind control port 8765".
   `xdg-open` launches exactly that, so the browser cannot serve as the system
   default until a second launch hands its URLs to the first.
5. `xdg-settings get default-web-browser` says `google-chrome.desktop` while the
   http/https mime handlers already say `claude-browser.desktop` — the two
   registries disagree, so "default" depends on which app asks.
6. `cb-mcp` is **not registered** as an MCP server anywhere, and the tools fail with
   "start it with `cb`" rather than starting it. Claude therefore falls back to the
   Chrome extension by default.

## Chunks — status updated as each lands

- [x] C1 — single API registry (`api.py`) + shared transport (`client.py`);
      regenerate control.py routes, cbctl, cb-mcp. Verify: unittest + live curl
      of every route against a running browser.
- [x] C2 — browser.py simplification: one GTK bridge, tab-lookup helper,
      load-url helper, Escape/close_panel, defensive-getattr cleanup.
      Verify: unittest + live browser run.
- [ ] C3 — second instance hands its URLs to the first and exits 0.
      Verify: launch two, confirm the URL lands as a tab in window one.
- [ ] C4 — default browser registration (`install.sh --set-default`) covering
      xdg-settings, mimeapps, XFCE helpers, BROWSER.
      Verify: `xdg-settings get`, `xdg-mime query`, real `xdg-open`.
- [ ] C5 — Claude debugs here by default: cb-mcp autostarts the browser,
      register at user scope, rewrite the web-browsing skill so Chrome is the
      documented fallback. Verify: MCP handshake + tools/call over stdio.
- [ ] C6 — prune the project's own context: README, stale docs and caches, so a
      fresh session reads less and none of it is out of date. (User request.)

## Outcomes

**C1** — `api.py` (one table, 20 ops) + `client.py` shared transport.
control.py 257->159, cbctl 134->80, cb-mcp 167->98. cb-mcp gained `forward`,
`wait`, `health`. Two bugs found and fixed by the live sweep, both mine:
`Gtk.Window.present()` takes no timestamp (`present_with_time` does), and
`cbctl shot <path>` had regressed to a `--path` flag.
Verified: 135 tests pass; all 20 routes exercised through `cbctl` against a real
browser on :0, PNG written to a file and streamed to stdout.

**C2** — one GTK bridge (`control.on_main_loop`) instead of two; `@needs_tab`
replaces seven copies of the same guard; `_load()` replaces four copies of the
navigate boilerplate *and fixes the omnibox*, which never called `_begin_load`,
so a typed navigation left the tab flagged idle and an agent's `wait` could
answer about the previous page. Escape now reuses `close_panel`.
Verified: same 135 tests, same live sweep.
