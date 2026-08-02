#!/usr/bin/env python3
"""Measure page-load time with and without the content blocker.

    python3 tools/bench.py

Launches the browser twice -- once with CB_BLOCK=0, once with CB_BLOCK=1 --
and times a real navigation of each URL through the control API.

Each run gets a fresh XDG_CACHE_HOME so both start with a cold HTTP cache;
otherwise whichever runs second wins on warm cache alone. The blocked run pays
for compiling the filter on its first launch, so the script warms that up
before timing.

Needs a display and a network. Times are wall-clock from "navigate" to WebKit
reporting the load finished, which is what the user actually waits for.
"""

import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Ordinary pages with a realistic amount of third-party weight. Kept to sites
# that do not require a login and are safe to hit repeatedly.
URLS = [
    "https://www.theverge.com/",
    "https://edition.cnn.com/",
    "https://developer.mozilla.org/en-US/docs/Web/CSS/flex",
    "https://en.wikipedia.org/wiki/WebKit",
]

REPEATS = 1


def wait_for_health(port, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=3):
                return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    return False


def post(port, path, payload, timeout=180):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path),
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def run(label, block, port):
    cache = tempfile.mkdtemp(prefix="cb-bench-")
    env = dict(os.environ, CB_BLOCK="1" if block else "0", XDG_CACHE_HOME=cache)
    proc = subprocess.Popen(
        [sys.executable, "-m", "claudebrowser", "--port", str(port), "about:blank"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    timings = {}
    try:
        if not wait_for_health(port):
            print("  %s: browser never came up" % label)
            return timings

        if block:
            # Compile the filter before timing anything, so the first URL is not
            # charged for it.
            post(port, "/open", {"url": "about:blank"})
            time.sleep(8)

        for url in URLS:
            samples = []
            for _ in range(REPEATS):
                start = time.time()
                try:
                    result = post(port, "/navigate", {"url": url, "wait": True})
                except Exception as e:
                    print("    %-46s FAILED (%s)" % (url[:46], e))
                    break
                elapsed = time.time() - start
                if not result.get("ok"):
                    print("    %-46s error: %s" % (url[:46], result.get("error")))
                    break
                samples.append(elapsed)
            if samples:
                timings[url] = min(samples)
                print("    %-46s %6.2fs" % (url[:46], min(samples)))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(cache, ignore_errors=True)
    return timings


def main():
    print("Benchmarking page load. A browser window will appear and close.\n")

    print("  blocker OFF")
    off = run("off", False, 8811)
    print("\n  blocker ON")
    on = run("on", True, 8812)

    shared = [u for u in URLS if u in off and u in on]
    if not shared:
        raise SystemExit("\nNo comparable samples -- check the network.")

    print("\n%-48s %8s %8s %8s" % ("page", "off", "on", "change"))
    print("-" * 76)
    speedups = []
    for url in shared:
        a, b = off[url], on[url]
        speedups.append(a / b if b else 1.0)
        print("%-48s %7.2fs %7.2fs %7.2fx" % (url[:48], a, b, a / b if b else 0))
    print("-" * 76)
    print("%-48s %7.2fs %7.2fs %7.2fx"
          % ("total", sum(off[u] for u in shared), sum(on[u] for u in shared),
             sum(off[u] for u in shared) / max(sum(on[u] for u in shared), 0.01)))
    print("\nmedian speedup: %.2fx" % statistics.median(speedups))


if __name__ == "__main__":
    main()
