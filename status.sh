#!/bin/sh
# Emits key=value lines describing the connected Pixel Buds. Cheap BlueZ check
# first; pbpctrl (RFCOMM to the buds) only runs when a pair is actually
# connected, so a run triggered by some other device's connect event costs
# one bluetoothctl call and nothing else.

dev=$(bluetoothctl devices Connected 2>/dev/null | grep -i 'pixel buds' | head -n1)
if [ -z "$dev" ]; then
  echo "connected=0"
  exit 0
fi

addr=$(printf '%s' "$dev" | awk '{print $2}')
name=$(printf '%s' "$dev" | cut -d' ' -f3-)
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
is_conn() { bluetoothctl info "$addr" 2>/dev/null | grep -q "Connected: yes"; }

is_conn || { echo "connected=0"; exit 0; }

rt=$(timeout 6 pbpctrl -d "$addr" show runtime 2>/dev/null) || {
  if is_conn; then echo "error=pbpctrl show runtime failed"; else echo "connected=0"; fi
  exit 0
}

# "  left bud:  100% (not charging)"  ->  left=100 / left_state=not charging
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
cache="${XDG_STATE_HOME:-$HOME/.local/state}/omarchy-pixelbuds-case"
case_now=$(printf '%s\n' "$parsed" | sed -n 's/^case=//p')
if [ -n "$case_now" ] && [ "$case_now" != "-1" ]; then
  mkdir -p "$(dirname "$cache")"
  printf '%s|%s\n' "$case_now" "$(date +%s)" > "$cache"
elif [ -f "$cache" ]; then
  IFS='|' read -r last_pct last_ts < "$cache"
  if [ -n "$last_pct" ] && [ -n "$last_ts" ]; then
    echo "case_last=$last_pct"
    echo "case_last_age=$(( $(date +%s) - last_ts ))"
  fi
fi

anc=$(timeout 6 pbpctrl -d "$addr" get anc 2>/dev/null | head -n1)
echo "anc=${anc:-unknown}"

# Optional second pass (--controls): device toggles and sound tuning, read
# only when the popup wants them. Every read is best-effort — a control the
# firmware doesn't answer for emits nothing, and the popup renders no row.
[ "${1:-}" = "--controls" ] || exit 0

# Same guard before the burst of control reads: never chase a leaving device.
is_conn || exit 0

for k in multipoint ohd speech-detection volume-exposure-notifications volume-eq mono; do
  v=$(timeout 6 pbpctrl -d "$addr" get "$k" 2>/dev/null | head -n1)
  case "$v" in
    true|false) echo "ctl_$(printf '%s' "$k" | tr - _)=$v" ;;
  esac
done

# "left: 100%, right: 80%" -> -100..100 (negative = toward the left)
bal=$(timeout 6 pbpctrl -d "$addr" get balance 2>/dev/null | head -n1)
case "$bal" in
  left:*)
    l=${bal#left: }; l=${l%%\%*}
    r=${bal##*right: }; r=${r%\%}
    echo "ctl_balance=$((r - l))"
    ;;
esac

# "[0.00, 1.50, ...]" -> comma-joined five bands
eqv=$(timeout 6 pbpctrl -d "$addr" get eq 2>/dev/null | head -n1)
case "$eqv" in
  \[*\]) echo "ctl_eq=$(printf '%s' "$eqv" | tr -d '[] ')" ;;
esac
