"""Named answering styles for the Claude panel.

Ask, TL;DR and Research share one system prompt, and that prompt carries
behaviour the browser depends on: answer from the page's text, say so when the
answer is not there, quote exact strings. A persona is a *second* paragraph
appended to it, never a replacement -- `compose` always starts from the base, so
no persona can drop the part that keeps answers grounded in the page.

Four of them, plus "off". They earn their place only by genuinely differing:
each one changes what the answer leads with and what it is allowed to leave out,
which is a real difference. None of them claim a capability the browser does not
have -- no persona may promise to run code, fetch another page, or remember a
previous session.

GTK-free, so the composition can be tested without a display. The active choice
lives in the settings file (`envfile`) rather than in memory, because a panel
mode you have to re-pick every launch is one you stop using.
"""

from . import envfile

#: The settings-file key. Read live on every request, like every other setting:
#: an edit to the file takes effect on the next question, with no restart.
SETTING = "CB_PERSONA"

DEFAULT = "off"

#: (key, label, prompt). Ordered as the panel shows them, with "off" first
#: because the unmodified prompt is the honest default -- a persona is opt-in.
PERSONAS = (
    ("off", "No persona", ""),

    ("developer", "Developer",
     "Answer as an engineer who has to make this work. Lead with the command, "
     "the code, or the exact identifier being asked about, then the one or two "
     "lines that explain it. Reproduce names, versions, flags and paths exactly "
     "as the page spells them, and quote code verbatim rather than paraphrasing "
     "it. Call out version constraints, breaking changes and prerequisites the "
     "page states. Skip analogies and background the reader already has."),

    ("researcher", "Researcher",
     "Answer as someone assessing how well this page supports what it claims. "
     "Separate what the page states outright from what it implies, and attribute "
     "each point to where on the page it came from. Note the evidence a claim "
     "rests on -- dates, numbers, sources, sample sizes -- and say plainly when a "
     "claim has none. End with what the page does not cover that a reader would "
     "need next."),

    ("critic", "Critic",
     "Read this adversarially. Lead with the weakest part of what the page "
     "argues or proposes: the unstated assumption, the failure mode, the cost "
     "that is not mentioned. Be specific about when and why it would break, not "
     "merely that it might. Say so directly when a point is well supported -- "
     "manufacturing an objection that the page has already answered is worse "
     "than having none."),

    ("translator", "Translator",
     "Answer in the language the user writes to you in, whatever language the "
     "page is in. When a specific wording matters, give the page's original text "
     "first and your rendering after it, so the reader can check it. Leave "
     "identifiers, code, commands, product names and proper nouns in their "
     "original form. Flag any phrase whose meaning you are unsure of rather than "
     "smoothing over it."),
)

BY_KEY = {key: (label, prompt) for key, label, prompt in PERSONAS}


def keys():
    return [key for key, _label, _prompt in PERSONAS]


def choices():
    """(key, label) pairs, in display order, for the panel's selector."""
    return [(key, label) for key, label, _prompt in PERSONAS]


def normalize(name):
    """The persona a name refers to, or None if there is no such persona.

    Case- and whitespace-insensitive so `cbctl persona Critic` works, and
    tolerant of the labels as well as the keys, because the label is what the
    panel shows and therefore what a person will type.
    """
    text = (name or "").strip().lower()
    if not text:
        return None
    if text in BY_KEY:
        return text
    for key, label, _prompt in PERSONAS:
        if text == label.lower():
            return key
    return None


def current(path=None):
    """The active persona key, read fresh from the settings file.

    An unrecognised value falls back to the default rather than failing: this is
    a hand-editable file, and a typo in it must cost a persona, not the panel.
    """
    return normalize(envfile.setting(SETTING, path=path)) or DEFAULT


def label(key):
    return BY_KEY.get(key, BY_KEY[DEFAULT])[0]


def prompt(key):
    return BY_KEY.get(key, BY_KEY[DEFAULT])[1]


def compose(base, key=None, path=None):
    """The system prompt for one request: the base, then the persona.

    The base is unconditional and comes first. A persona can only add a
    paragraph after it -- there is no key, present or absent, valid or
    mistyped, that produces a prompt without the browser's own instructions in
    it.
    """
    key = key if key is not None else current(path)
    extra = prompt(key)
    if not extra:
        return base
    return "%s\n\n%s" % (base, extra)


def remember(name, path=None):
    """Persist the choice and return the key that was stored.

    Written to the settings file rather than held in memory for the same reason
    the API key lives there: the browser is launched from a menu, a terminal and
    a dock, and it has to behave the same from all three.
    """
    key = normalize(name)
    if key is None:
        raise ValueError("no such persona %r; try %s"
                         % (name, ", ".join(keys())))
    envfile.put(SETTING, key, path=path)
    return key


def describe(path=None):
    """What the persona ops and the panel both report."""
    key = current(path)
    return {"persona": key, "label": label(key),
            "available": [{"name": k, "label": lb} for k, lb in choices()]}
