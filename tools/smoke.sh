#!/usr/bin/env bash
# Exercise the agent-facing operations against a RUNNING browser.
#
# The unit suite cannot touch WebKit, so this is the one check that the
# display-side half of every op actually works: it opens tests/fixtures/smoke.html
# in the live browser and drives it with cbctl, asserting on what comes back.
# Run it after any change to browser.py or extract.py:
#
#     tools/smoke.sh            # needs the browser up: `cbctl health` first
#
# It opens one tab and closes it at the end. It writes only under $TMPDIR (or
# /tmp) and under ~/.cache/claude-browser/smoke/.

set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
CB="$HERE/cbctl"
PAGE="file://$HERE/tests/fixtures/smoke.html"
OUT="${HOME:?}/.cache/claude-browser/smoke"
mkdir -p "$OUT"

pass=0; fail=0
ok()   { pass=$((pass + 1)); printf '  ok   %s\n' "$1"; }
bad()  { fail=$((fail + 1)); printf '  FAIL %s\n       %s\n' "$1" "$2"; }
# expect NAME 'pattern' -- the last command's JSON must match the regex
check() { if printf '%s' "$2" | grep -Eq "$3"; then ok "$1"; else bad "$1" "$2"; fi; }

"$CB" health >/dev/null || { echo "browser is not running (cbctl health failed)"; exit 2; }

r=$("$CB" open "$PAGE"); check open "$r" '"ok": true'
TAB=$(printf '%s' "$r" | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["id"])' 2>/dev/null \
      || printf '%s' "$r" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
T=(--tab "$TAB")

r=$("$CB" "${T[@]}" wait-for --selector '#late' --timeout 10); check "wait-for selector (appears after 1.5s)" "$r" '"matched": "selector"'
r=$("$CB" "${T[@]}" wait-for --idle);                      check "wait-for idle" "$r" '"ok": true'
r=$("$CB" "${T[@]}" wait-for --selector '#nope' --timeout 2 || true); check "wait-for timeout is honest" "$r" 'timed out'

r=$("$CB" "${T[@]}" state);                     check "state has a token" "$r" '"token": "[a-z0-9]+:[0-9]+"'
r=$("$CB" "${T[@]}" text);                      check "text carries state" "$r" '"token"'
r=$("$CB" "${T[@]}" eval "document.getElementById('intro').insertAdjacentHTML('afterend','<p>Freshly added line</p>')" >/dev/null)
r=$("$CB" "${T[@]}" changes);                   check "changes shows the added line" "$r" 'Freshly added line'
r=$("$CB" "${T[@]}" changes);                   check "changes is empty once read" "$r" '"added": \[\]'
TOK=$("$CB" "${T[@]}" state | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
"$CB" "${T[@]}" eval "setTimeout(function(){document.body.appendChild(document.createElement('p'))},400)" >/dev/null
r=$("$CB" "${T[@]}" wait-for --changed_since "$TOK" --timeout 5); check "wait-for --changed-since" "$r" '"matched": "change"'
r=$("$CB" "${T[@]}" text --selector '#intro');  check "text --selector" "$r" 'tools/smoke.sh'
r=$("$CB" "${T[@]}" find 'Banana');             check "find" "$r" '"count": 1'
r=$("$CB" "${T[@]}" tables);                    check "tables" "$r" 'Banana'

r=$("$CB" "${T[@]}" scroll --to bottom);             check "scroll --to bottom" "$r" '"at_bottom": true'
r=$("$CB" "${T[@]}" scroll --to top);                check "scroll --to top" "$r" '"y": 0'
r=$("$CB" "${T[@]}" scroll --to '#bottom');          check "scroll to selector" "$r" '"ok": true'

r=$("$CB" "${T[@]}" fill '#q' 'hello');         check "fill" "$r" '"ok": true'
r=$("$CB" "${T[@]}" type ' world' --selector '#q'); check "type appends" "$r" '"ok": true'
r=$("$CB" "${T[@]}" type 'replaced' --selector '#q' --clear); check "type --clear replaces" "$r" '"ok": true'
r=$("$CB" "${T[@]}" eval "document.getElementById('q').value"); check "field holds only the new text" "$r" '"replaced"'
r=$("$CB" "${T[@]}" clear-field '#q');          check "clear-field" "$r" '"ok": true'
r=$("$CB" "${T[@]}" eval "document.getElementById('q').value"); check "field is empty" "$r" '"result": ""'
r=$("$CB" "${T[@]}" fill '#q' 'hello');         check "fill again" "$r" '"ok": true'
r=$("$CB" "${T[@]}" type ' world' --selector '#q'); check "type appends again" "$r" '"ok": true'
r=$("$CB" "${T[@]}" select '#c' 'Green');       check "select by label" "$r" '"value": "g"'
r=$("$CB" "${T[@]}" select '#k' --checked);     check "select checkbox" "$r" '"checked": true'
r=$("$CB" "${T[@]}" press Enter --selector '#q'); check "press Enter submits" "$r" '"ok": true'
r=$("$CB" "${T[@]}" text --selector '#out');    check "form saw the values" "$r" 'submitted:hello world:g:on'

r=$("$CB" "${T[@]}" hover '#menu-label');       check "hover" "$r" '"ok": true'
r=$("$CB" "${T[@]}" eval "getComputedStyle(document.getElementById('menu-items')).display"); check "hover opened the menu (CSS :hover is pointer-driven; a synthetic hover may not)" "$r" '"result"'

r=$("$CB" "${T[@]}" click '#ask');              check "click confirm button" "$r" '"ok": true'
r=$("$CB" "${T[@]}" dialogs);                   check "dialog was auto-answered and logged" "$r" '"kind": "confirm"'
r=$("$CB" "${T[@]}" text --selector '#out');    check "confirm returned true" "$r" 'confirm:true'

printf 'smoke upload\n' > "$OUT/upload.txt"
r=$("$CB" "${T[@]}" upload '#file' "$OUT/upload.txt"); check "upload" "$r" 'upload.txt'
r=$("$CB" "${T[@]}" text --selector '#out');    check "page saw the file" "$r" 'files:1'

r=$("$CB" "${T[@]}" network);                   check "network log has the document" "$r" 'smoke.html'
r=$("$CB" "${T[@]}" shot "$OUT/full.png" --full);           check "screenshot --full" "$r" '"ok": true'
r=$("$CB" "${T[@]}" shot "$OUT/table.png" --selector '#t'); check "screenshot --selector" "$r" '"ok": true'
r=$("$CB" "${T[@]}" pdf "$OUT/page.pdf" --overwrite); check "pdf" "$r" '"ok": true'
[ -s "$OUT/page.pdf" ] && ok "pdf file is non-empty" || bad "pdf file is non-empty" "$(ls -la "$OUT")"

r=$("$CB" "${T[@]}" download "$PAGE" "$OUT/downloaded.html" --overwrite); check "download" "$r" '"ok": true'
grep -q 'Smoke page' "$OUT/downloaded.html" && ok "download content" || bad "download content" "$(head -c 200 "$OUT/downloaded.html")"

r=$("$CB" "${T[@]}" submit '#f');               check "submit (no navigation)" "$r" '"navigated": false'

# The freshness gate: a page that moved after a look older than FRESH_S refuses an act.
"$CB" "${T[@]}" text >/dev/null
"$CB" "${T[@]}" eval "setTimeout(function(){document.body.appendChild(document.createElement('p'))},200)" >/dev/null
printf '  (sleeping 21s for the freshness window)\n'; sleep 21
r=$("$CB" "${T[@]}" click '#ask' 2>&1 || true); check "stale act is refused with the reason" "$r" 'changed [0-9]+ times'
r=$("$CB" "${T[@]}" click '#ask');              check "act after the refusal goes through" "$r" '"ok": true'
r=$("$CB" "${T[@]}" click '#ask' --force);      check "force bypasses the gate" "$r" '"ok": true'

r=$("$CB" "${T[@]}" close);                     check "close" "$r" '"ok": true'

echo
echo "passed $pass, failed $fail  (artifacts in $OUT)"
[ "$fail" -eq 0 ]
