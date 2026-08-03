# Unbuilt ideas that fit this stack

A shortlist, not a brainstorm. Everything here is buildable in what this project
already is — Python 3 + GTK3 + WebKit2GTK, standard library only, no pip, no
node, no build step. Anything that needed a different product (a Tauri/React
front end, a Rust or FastAPI service, an embedding model, an opt-in VPS with
Postgres and Playwright) has been removed rather than parked; the reasoning for
those refusals lives in `CLAUDE.md` under *Architectures already rejected*, so
it does not get re-proposed here every six months.

Ideas that have since been built — reader mode, the page-text cache, `recall`
full-text search, the visible agent cursor — are documented in `README.md` and
`CLAUDE.md` and are no longer suggestions.

## Tab snapshot summaries

A discarded tab currently reloads from nothing when you come back to it, and in
the deck it is a title and a site mark. Give the resource guard's discard path a
one-line summary to leave behind, so a dropped tab still says what it was.

The text is already on disk — `pagetext.text_for(url)` has the article for any
page that finished loading — so this is a summarisation and a place to put the
result, not a new capture path. Cache the summary keyed by content hash, the
same way `pagetext` keys bodies, so revisiting an article does not pay for it
twice. Show it on the `cb:deck` card and in the tab tooltip.

Open question worth deciding before starting: whether the summary is generated
eagerly on discard (costs an API call for a tab you may never return to) or
lazily the first time the deck renders a discarded card.

## PII scrubber and an outbound prompt preview

Ask, TL;DR, Research and the agent loop all send page text to Anthropic. Nothing
currently shows the user what is about to leave the machine.

Two halves:

- A local scrubber over the text about to be sent — email addresses, phone
  numbers, card-shaped digit runs, anything in a field the password layer
  recognises — highlighted with one-tap redaction. Regex and `passwords.py`'s
  existing field knowledge; no model.
- A preview step in the Claude panel for the large sends (a full page dump,
  Research across every open tab), with the redactions applied and editable
  before it goes.

Fold in the known gap while here: `pagetext` has `clear()` and `forget()` and
nothing calls either. Wire them into `cb:data` and into `cbctl clear`, so the
page-text cache is clearable like cookies and the disk cache already are.

## Playbooks: record and replay command sequences

`api.py` already describes every operation as data, and `cbctl` already turns
that table into a CLI. A playbook is a saved list of those calls — open, fill,
click, read — replayed against the live browser with the user's own session.

The cheap version is a JSON file per playbook in the data directory, a `cbctl
playbook record|run|list` subcommand family, and replay that goes through the
same `_admit` queue every other API-initiated load does. Parameters are the
interesting part: a recorded run hard-codes the URL and the search term it used,
and a playbook worth keeping takes them as arguments.

## Claude personas

Ask, TL;DR and Research share one system prompt. A persona is a named prompt
preset — Developer, Researcher, Critic, Translator — chosen from a pill in the
Claude panel, changing tone and what the answer leads with.

Small and self-contained: prompts live beside the existing ones, the selection
is a panel control, and the current persona is part of the panel's state. Worth
doing only if the four prompts genuinely differ; four labels over one prompt is
a worse product than one honest mode.

## Smaller things, in rough order of value

- **Keep-alive HTTPS connection to the API.** `ai.py` opens a fresh TLS
  connection per request; a run of agent steps pays a handshake each time. One
  pooled connection, plus a DNS/TLS preconnect at startup, is stdlib work with a
  measurable latency win on a slow uplink.
- **Read-later cache from what is already stored.** The page-text cache holds
  the prose of everything read; reader mode can already render an article. An
  offline "read later" view is mostly a `cb:` page over `pagetext`, not a new
  subsystem.
- **Inline hover badges.** A one-line TL;DR on hovering a link, served only from
  the page-text cache — never a live API call on hover, which would be a request
  per mouse movement.
- **Recall in the omnibox.** The address bar ranks history and bookmarks by
  title and URL. `recall` searches the full text of the same pages. Merging the
  two makes "the page about rate limits I read yesterday" findable by a phrase
  that never appeared in its title.
- **Battery and idle awareness in the resource guard.** `resources.py` already
  reads `/proc`; background work (summaries, indexing) could be gated on being
  plugged in and idle, the same way tab discarding is gated on swap-in rate.
- **Lazy images and iframes.** A `loading="lazy"` pass and a low-quality
  placeholder for offscreen images, on hardware where a page's image decode is a
  real cost.
