#!/bin/sh
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
mkdir "$tmp/bin" "$tmp/runtime"

cat >"$tmp/bin/pbpctrl" <<'EOF'
#!/bin/sh
printf 'start\n' >>"$PBPCTRL_TEST_LOG"
sleep 1
printf 'end\n' >>"$PBPCTRL_TEST_LOG"
printf '%s\n' "$*"
EOF
chmod +x "$tmp/bin/pbpctrl"

export PATH="$tmp/bin:$PATH"
export XDG_RUNTIME_DIR="$tmp/runtime"
export PBPCTRL_TEST_LOG="$tmp/events"

"$repo/pbpctrl-locked.sh" -d AA:BB:CC:DD:EE:FF get anc >"$tmp/first" &
first=$!
"$repo/pbpctrl-locked.sh" -d 11:22:33:44:55:66 show runtime >"$tmp/second" &
second=$!
wait "$first"
wait "$second"

events=$(tr '\n' ' ' <"$tmp/events")
[ "$events" = "start end start end " ] || {
  echo "pbpctrl calls overlapped: $events" >&2
  exit 1
}

grep -Fx -- '-d AA:BB:CC:DD:EE:FF get anc' "$tmp/first" >/dev/null
grep -Fx -- '-d 11:22:33:44:55:66 show runtime' "$tmp/second" >/dev/null
echo "pbpctrl lock test passed"
