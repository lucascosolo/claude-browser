"""Where the agent's file operations may read and write.

`upload` hands a local file to a page, and `download` and `pdf` write files
named by the caller. All three are MCP tools, so the caller is a model that may
be reading a hostile page; without a boundary, "upload ~/.ssh/id_ed25519" or
"download to ~/.bashrc" is one prompt injection away. CB_AGENT_DIRS names the
directories they are confined to. GTK-free, so the rule is tested.

Symlinks are resolved before the check -- a link inside a root that points out
of it is outside, which is the case a prefix test on the spelled path misses.
"""
import os

DEFAULT = "~/Downloads:~/.cache/claude-browser"


def roots(value):
    """The realpath'd absolute directories in a colon-separated setting.
    Unset, or nothing usable in it, means DEFAULT."""
    found = []
    for entry in (value or "").split(":"):
        entry = os.path.expanduser(entry.strip())
        if entry and os.path.isabs(entry):
            found.append(os.path.realpath(entry))
    if not found and value != DEFAULT:
        return roots(DEFAULT)
    return found


def _check(path, real, roots, strict):
    for root in roots:
        root = os.path.realpath(root)
        if real.startswith(root.rstrip(os.sep) + os.sep) or (not strict and real == root):
            return real
    raise ValueError("outside CB_AGENT_DIRS (%s): %s" % (":".join(roots), path))


def _absolute(path):
    if not path or not os.path.isabs(path):
        raise ValueError("not an absolute path: %s" % (path,))


def contain_read(path, roots):
    """The resolved `path` if it lies under one of `roots`, else ValueError."""
    _absolute(path)
    return _check(path, os.path.realpath(path), roots, strict=False)


def contain_write(path, roots):
    """The resolved destination if it lies strictly under one of `roots`.
    realpath follows a final-component symlink, dangling or not, so a planted
    link cannot redirect the write out of the root."""
    _absolute(path)
    return _check(path, os.path.realpath(path), roots, strict=True)
