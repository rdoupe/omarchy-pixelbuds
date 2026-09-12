#!/bin/sh
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
mkdir "$tmp/bin" "$tmp/runtime"
chmod 700 "$tmp/runtime"

cat >"$tmp/bin/pbpctrl" <<'EOF'
#!/bin/sh
printf 'start\n' >>"$XDG_RUNTIME_DIR/pbpctrl-events"
sleep 1
printf 'end\n' >>"$XDG_RUNTIME_DIR/pbpctrl-events"
printf '%s\n' "$*"
EOF
chmod +x "$tmp/bin/pbpctrl"

# Stubs are selected by the trusted-dir allowlist. The child env is closed,
# so the fake pbpctrl records overlap via XDG_RUNTIME_DIR, not extra vars.
export PIXELBUDS_TRUSTED_PATH="$tmp/bin"
export PATH="/usr/bin:/bin"
export XDG_RUNTIME_DIR="$tmp/runtime"

"$repo/pbpctrl-locked.sh" -d AA:BB:CC:DD:EE:FF get anc >"$tmp/first" &
first=$!
"$repo/pbpctrl-locked.sh" -d 11:22:33:44:55:66 show runtime >"$tmp/second" &
second=$!
wait "$first"
wait "$second"

events=$(tr '\n' ' ' <"$tmp/runtime/pbpctrl-events")
[ "$events" = "start end start end " ] || {
  echo "pbpctrl calls overlapped: $events" >&2
  exit 1
}

grep -Fx -- '-d AA:BB:CC:DD:EE:FF get anc' "$tmp/first" >/dev/null
grep -Fx -- '-d 11:22:33:44:55:66 show runtime' "$tmp/second" >/dev/null
echo "pbpctrl lock test passed"
