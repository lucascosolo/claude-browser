"""claudebrowser/chrome_import.py

Read-only import from a local Google Chrome profile: bookmarks and history.
GTK-free so it is testable without a display, following the existing
convention of playbooks.py / profile.py / fills.py.

Every function here reads from a *copy* of Chrome's files, never the live
one -- Chrome holds History and Login Data open, and a plain filesystem copy
sidesteps that without needing Chrome closed. The copy lives in a
TemporaryDirectory and is always cleaned up, even on an exception, so a crash
mid-import never leaves a copy of the user's browsing history sitting in /tmp.

read_passwords is the one function that touches encrypted data, and it is
this module's entire reason for existing carefully: see chrome_import.py's
own docstring on read_passwords for the constraint that governs it.
"""

import json
import os
import shutil
import sqlite3
import tempfile
from hashlib import pbkdf2_hmac

from claudebrowser import _aes

CHROME_PROFILE_DEFAULT = os.path.expanduser("~/.config/google-chrome/Default")

# Seconds between the Windows FILETIME epoch (1601-01-01) Chrome timestamps
# use and the Unix epoch (1970-01-01) every other part of this browser uses.
CHROME_EPOCH_OFFSET_SECONDS = 11644473600

# Chrome's own schema for the "Chrome Safe Storage" libsecret item (from
# Chromium's components/os_crypt/key_storage_libsecret.cc), confirmed against
# this machine's keyring via a metadata-only search -- see read_passwords.
CHROME_SAFE_STORAGE_SCHEMA = "chrome_libsecret_os_crypt_password_v2"
CHROME_APPLICATION_CANDIDATES = ("chrome", "google-chrome", "chromium")

# Chromium's own documented fallback: if no OS keyring was available the
# first time Chrome ran, it encrypts with this literal passphrase instead of
# refusing to store passwords at all.
CHROME_FALLBACK_SECRET = b"peanuts"


def _chrome_time_to_epoch(chrome_us):
    """Chrome stores timestamps as microseconds since 1601-01-01. 0 means
    "never set" in Chrome's own data and maps to Unix epoch 0 here too."""
    chrome_us = int(chrome_us or 0)
    if not chrome_us:
        return 0
    return int(chrome_us / 1_000_000 - CHROME_EPOCH_OFFSET_SECONDS)


# A signed-in Chrome profile with bookmark account storage keeps local and
# account-synced bookmarks in two separate, identically-shaped JSON files --
# a profile can have either, both, or (rarely) neither, so both are read and
# merged rather than assuming the classic file is the only one that exists.
BOOKMARK_FILES = ("Bookmarks", "AccountBookmarks")

# Same split for passwords: a signed-in profile keeps device-local logins in
# Login Data and account-synced ones in Login Data For Account. On a
# profile signed into a Google Account, most or all real logins are typically
# in the *For Account* file, with Login Data near-empty -- reading only the
# classic file silently imports nothing.
LOGIN_DATA_FILES = ("Login Data", "Login Data For Account")


def read_bookmarks(profile_dir=CHROME_PROFILE_DEFAULT):
    """Chrome's Bookmarks/AccountBookmarks files are plain JSON, never
    encrypted. Walks every root (bookmark_bar, other, synced) in whichever of
    BOOKMARK_FILES exist, and returns (url, title, added_epoch) for every
    url-type node, folders included at any depth. A url present in both
    files is reported once, from whichever file is read first."""
    seen = set()
    out = []

    def walk(node):
        if node.get("type") == "url":
            url = node.get("url", "")
            if url and url not in seen:
                seen.add(url)
                out.append((
                    url,
                    node.get("name", ""),
                    _chrome_time_to_epoch(node.get("date_added", 0)),
                ))
        for child in node.get("children", ()):
            walk(child)

    for filename in BOOKMARK_FILES:
        path = os.path.join(profile_dir, filename)
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for root in data.get("roots", {}).values():
            if isinstance(root, dict):
                walk(root)
    return out


def read_history(profile_dir=CHROME_PROFILE_DEFAULT, limit=100000):
    """Chrome's History file is a plain, unencrypted SQLite database that
    Chrome typically holds open -- copied to a temp dir first so a read never
    waits on or disturbs Chrome's own lock, and the copy never outlives this
    call."""
    path = os.path.join(profile_dir, "History")
    with tempfile.TemporaryDirectory(prefix="cb-chrome-import-") as tmp:
        copy_path = os.path.join(tmp, "History")
        shutil.copy2(path, copy_path)
        conn = sqlite3.connect(copy_path)
        try:
            rows = conn.execute(
                "SELECT url, title, visit_count, last_visit_time "
                "FROM urls ORDER BY last_visit_time DESC LIMIT ?", (limit,)
            ).fetchall()
        finally:
            conn.close()
    return [
        (url, title or "", visits or 0, _chrome_time_to_epoch(last_visit))
        for url, title, visits, last_visit in rows
    ]


def _chrome_safe_storage_secret():
    """The Chrome Safe Storage passphrase: from the keyring if Chrome ever
    stored one there, else Chromium's own documented fallback for a machine
    that had no keyring available when Chrome first ran.

    Metadata-only search first (password_search_sync, never LOAD_SECRETS) so
    a wrong application-name guess can never fetch a value this call does not
    need -- password_lookup_sync (which does return the secret) is only
    reached once a specific attribute set is already confirmed to match an
    item. See Task 3's "Precondition check" in the implementation plan for
    why this uses the simple password API rather than Collection.get_items().
    """
    import gi
    gi.require_version("Secret", "1")
    from gi.repository import Secret

    schema = Secret.Schema.new(
        CHROME_SAFE_STORAGE_SCHEMA, Secret.SchemaFlags.NONE,
        {"application": Secret.SchemaAttributeType.STRING})
    for application in CHROME_APPLICATION_CANDIDATES:
        attrs = {"application": application}
        found = Secret.password_search_sync(schema, attrs, Secret.SearchFlags.ALL, None)
        if found:
            secret = Secret.password_lookup_sync(schema, attrs, None)
            if secret:
                return secret.encode("utf-8")
    return CHROME_FALLBACK_SECRET


def _read_logins_table(path):
    """Copy one Chrome logins database and return its raw (origin, username,
    encrypted password blob) rows -- no decryption here, so this half can be
    exercised without a secret at all."""
    with tempfile.TemporaryDirectory(prefix="cb-chrome-import-") as tmp:
        copy_path = os.path.join(tmp, os.path.basename(path))
        shutil.copy2(path, copy_path)
        conn = sqlite3.connect(copy_path)
        try:
            return conn.execute(
                "SELECT origin_url, username_value, password_value FROM logins"
            ).fetchall()
        finally:
            conn.close()


def read_passwords(profile_dir=CHROME_PROFILE_DEFAULT, secret=None):
    """Yields (origin, username, password) for every decryptable row across
    whichever of LOGIN_DATA_FILES exist. A generator, never a list -- see the
    module docstring and the plan's global constraints on why a decrypted
    value never accumulates anywhere.

    `secret` overrides the keyring lookup entirely when given (bytes) -- this
    is the seam every test in this suite uses; production code always omits
    it and gets Chrome's real key via _chrome_safe_storage_secret().

    A row whose prefix is not v10/v11, or that fails to decrypt under the
    given secret (wrong key, corrupt row, a future Chrome scheme this
    function does not recognise), is silently skipped rather than raised --
    one bad row must never abort the whole import.
    """
    if secret is None:
        secret = _chrome_safe_storage_secret()
    key = pbkdf2_hmac("sha1", secret, b"saltysalt", 1, dklen=16)
    for filename in LOGIN_DATA_FILES:
        path = os.path.join(profile_dir, filename)
        if not os.path.isfile(path):
            continue
        for origin, username, encrypted in _read_logins_table(path):
            if not encrypted or encrypted[:3] not in (b"v10", b"v11"):
                continue
            try:
                padded = _aes.aes128_cbc_decrypt(key, b" " * 16, encrypted[3:])
                password = _aes.strip_pkcs7(padded).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                continue
            yield origin, username or "", password
