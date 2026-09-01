#!/bin/sh
# Emits key=value lines describing the connected Pixel Buds. Cheap BlueZ check
# first; pbpctrl (RFCOMM to the buds) only runs when a pair is actually
# connected, so a run triggered by some other device's connect event costs
# one bluetoothctl call and nothing else.
#
# Everything read from bluetoothctl or pbpctrl is device-controlled, so every
# capture is byte-bounded, runs under a deadline, and is killable: a TERM from
# the shell (a user disconnect) stops the in-flight pbpctrl immediately.

tmpd=$(mktemp -d) || exit 0
child=""
cleanup() { [ -n "$child" ] && kill "$child" 2>/dev/null; rm -rf "$tmpd"; }
trap 'cleanup; exit 143' TERM INT HUP
trap cleanup EXIT

# cap <maxbytes> <cmd...>: run cmd as a killable child, keep at most maxbytes
# of its stdout in $out, return its exit status.
cap() {
  max=$1; shift
  "$@" >"$tmpd/o" 2>/dev/null & child=$!
  wait "$child"; rc=$?; child=""
  out=$(head -c "$max" "$tmpd/o")
  return $rc
}

# line1 <text> <maxchars>: first line, truncated.
line1() { printf '%s\n' "$1" | head -n1 | cut -c1-"$2"; }

is_num() { printf '%s' "$1" | grep -Eq '^[0-9]{1,3}$' && [ "$1" -le 100 ]; }

cap 8192 timeout 5 bluetoothctl devices Connected || out=""
dev=$(printf '%s\n' "$out" | grep -i 'pixel buds' | head -n1 | cut -c1-200)
if [ -z "$dev" ]; then
  echo "connected=0"
  exit 0
fi

addr=$(printf '%s' "$dev" | awk '{print $2}')
name=$(printf '%s' "$dev" | cut -d' ' -f3- | cut -c1-100)
printf '%s' "$addr" | grep -Eq '^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$' || {
  echo "connected=0"
  exit 0
}
echo "connected=1"
echo "addr=$addr"
echo "name=$name"

# Buds are connected: surface a missing pbpctrl instead of hiding the widget,
# so a fresh install isn't just silently invisible.
if ! command -v pbpctrl >/dev/null 2>&1; then
  echo "missing_pbpctrl=1"
  echo "error=pbpctrl is not installed"
  exit 0
fi

# pbpctrl opens its own RFCOMM session, and doing that against a device that
# is mid-disconnect re-establishes the link — the plugin must never be the
# reason the buds refuse to let go. So re-verify the link right before
# talking, and treat a failure followed by a gone link as a plain disconnect
# (a later connected=0 overrides the connected=1 printed above).
is_conn() {
  cap 8192 timeout 5 bluetoothctl info "$addr" && printf '%s\n' "$out" | grep -q 'Connected: yes'
}

is_conn || { echo "connected=0"; exit 0; }

if ! cap 8192 timeout 6 pbpctrl -d "$addr" show runtime; then
  if is_conn; then echo "error=pbpctrl show runtime failed"; else echo "connected=0"; fi
  exit 0
fi
rt=$out

# "  left bud:  100% (not charging)"  ->  left=100 / left_state=not charging
# Percentages must be 0-100 and states short lowercase words; anything else
# is reported as unknown.
parsed=$(printf '%s\n' "$rt" | awk '
  /^battery:/   { sec = "bat"; next }
  /^placement:/ { sec = "place"; next }
  /^connection:/{ sec = "conn"; next }
  sec == "bat" && /^  (case|left bud|right bud):/ {
    key = ($1 == "case:") ? "case" : $1
    sub(/:$/, "", key)
    line = $0; sub(/^  [a-z ]+: +/, "", line)
    if (line == "unknown") { print key "=-1"; print key "_state=unknown"; next }
    pct = line; sub(/%.*/, "", pct)
    st = line; sub(/^[0-9]+% \(/, "", st); sub(/\)$/, "", st)
    if (pct !~ /^[0-9][0-9]?[0-9]?$/ || pct + 0 > 100) pct = -1
    if (st !~ /^[a-z ]{1,24}$/) st = "unknown"
    print key "=" pct
    print key "_state=" st
  }
  sec == "place" && /^  (left|right) bud:/ {
    key = $1
    line = $0; sub(/^  [a-z ]+: +/, "", line)
    print key "_in_case=" ((line == "in case") ? 1 : 0)
  }
')
printf '%s\n' "$parsed"

# The case only reports through a docked bud (it has no radio of its own), so
# mirror Android: remember the last reading and surface it while it is stale.
# The cache lives in a private 0700 directory of our own, is written through a
# 0600 temp file plus atomic rename onto a verified non-symlink regular file,
# and is only read back when it is a small regular file holding two sane
# numbers.
sdir="${XDG_STATE_HOME:-$HOME/.local/state}/omarchy-pixelbuds"
cache="$sdir/case"
now=$(date +%s)
case_now=$(printf '%s\n' "$parsed" | sed -n 's/^case=//p' | head -n1)
if is_num "$case_now"; then
  if { [ -d "$sdir" ] || mkdir -m 0700 "$sdir" 2>/dev/null; } && [ ! -L "$sdir" ] \
     && [ ! -L "$cache" ] && { [ ! -e "$cache" ] || [ -f "$cache" ]; }; then
    chmod 0700 "$sdir" 2>/dev/null
    if tmp=$(mktemp "$sdir/.case.XXXXXX" 2>/dev/null); then
      printf '%s|%s\n' "$case_now" "$now" > "$tmp" && mv -f "$tmp" "$cache" || rm -f "$tmp"
    fi
  fi
elif [ ! -L "$cache" ] && [ -f "$cache" ] && [ "$(wc -c < "$cache")" -le 64 ]; then
  IFS='|' read -r last_pct last_ts < "$cache"
  if is_num "$last_pct" && printf '%s' "$last_ts" | grep -Eq '^[0-9]{1,12}$' \
     && [ "$last_ts" -le "$now" ]; then
    echo "case_last=$last_pct"
    echo "case_last_age=$((now - last_ts))"
  fi
fi

cap 256 timeout 6 pbpctrl -d "$addr" get anc || out=""
anc=$(line1 "$out" 16)
case "$anc" in off|active|aware|adaptive) ;; *) anc=unknown ;; esac
echo "anc=$anc"

# Optional second pass (--controls): device toggles and sound tuning, read
# only when the popup wants them. Every read is best-effort — a control the
# firmware doesn't answer for emits nothing, and the popup renders no row.
[ "${1:-}" = "--controls" ] || exit 0

# Same guard before the burst of control reads: never chase a leaving device.
is_conn || exit 0

for k in multipoint ohd speech-detection volume-exposure-notifications volume-eq mono; do
  cap 256 timeout 6 pbpctrl -d "$addr" get "$k" || out=""
  v=$(line1 "$out" 8)
  case "$v" in
    true|false) echo "ctl_$(printf '%s' "$k" | tr - _)=$v" ;;
  esac
done

# "left: 100%, right: 80%" -> -100..100 (negative = toward the left)
cap 256 timeout 6 pbpctrl -d "$addr" get balance || out=""
bal=$(line1 "$out" 64)
case "$bal" in
  left:*)
    l=${bal#left: }; l=${l%%\%*}
    r=${bal##*right: }; r=${r%\%}
    if is_num "$l" && is_num "$r"; then
      echo "ctl_balance=$((r - l))"
    fi
    ;;
esac

# "[0.00, 1.50, ...]" -> comma-joined five bands, each a plain decimal
cap 256 timeout 6 pbpctrl -d "$addr" get eq || out=""
eqv=$(line1 "$out" 96)
case "$eqv" in
  \[*\])
    eqc=$(printf '%s' "$eqv" | tr -d '[] ')
    printf '%s' "$eqc" | grep -Eq '^-?[0-9]{1,2}(\.[0-9]{1,2})?(,-?[0-9]{1,2}(\.[0-9]{1,2})?){4}$' \
      && echo "ctl_eq=$eqc"
    ;;
esac
