#!/bin/sh
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
mkdir "$tmp/bin" "$tmp/runtime" "$tmp/state"
chmod 700 "$tmp/runtime"

cat >"$tmp/bin/bluetoothctl" <<'EOF'
#!/bin/sh
case "$*" in
  "devices Connected") echo "Device AA:BB:CC:DD:EE:FF Pixel Buds Pro" ;;
  "info AA:BB:CC:DD:EE:FF") echo "Connected: yes" ;;
  *) exit 1 ;;
esac
EOF

cat >"$tmp/bin/pbpctrl" <<'EOF'
#!/bin/sh
if [ "$*" = "set anc --help" ]; then
  if [ "${PBPCTRL_TEST_ADAPTIVE:-0}" = 1 ]; then
    echo "possible values: off, active, aware, adaptive"
  else
    echo "possible values: off, active, aware"
  fi
  exit 0
fi

[ "$1" = "-d" ] && shift 2
case "$*" in
  "show runtime")
    cat <<'OUT'
battery:
  case: 75% (not charging)
  left bud: 90% (not charging)
  right bud: 85% (charging)
placement:
  left bud: in ear
  right bud: in case
connection:
  state: connected
OUT
    ;;
  "get anc") echo active ;;
  "get multipoint") echo true ;;
  "get ohd") echo true ;;
  "get speech-detection") echo false ;;
  "get volume-exposure-notifications") echo true ;;
  "get volume-eq") echo false ;;
  "get mono") echo false ;;
  "get gestures") echo true ;;
  "get gesture-control") echo "left: anc, right: assistant" ;;
  "get anc-gesture-loop")
    if [ "${PBPCTRL_TEST_ADAPTIVE:-0}" = 1 ]; then
      echo "[active, aware, adaptive]"
    else
      echo "[active, aware]"
    fi
    ;;
  "get balance") echo "left: 80%, right: 100%" ;;
  "get eq") echo "[0.00, 1.50, -2.00, 0.50, 3.00]" ;;
  *) exit 1 ;;
esac
EOF

chmod +x "$tmp/bin/bluetoothctl" "$tmp/bin/pbpctrl"
export PATH="$tmp/bin:$PATH"
export XDG_RUNTIME_DIR="$tmp/runtime"
export XDG_STATE_HOME="$tmp/state"

assert_line() {
  printf '%s\n' "$1" | grep -Fx -- "$2" >/dev/null || {
    echo "missing output: $2" >&2
    exit 1
  }
}

legacy=$(PBPCTRL_TEST_ADAPTIVE=0 "$repo/status.sh" --controls)
assert_line "$legacy" "connected=1"
assert_line "$legacy" "adaptive_supported=0"
assert_line "$legacy" "anc=active"
assert_line "$legacy" "ctl_gestures=true"
assert_line "$legacy" "ctl_gesture_left=anc"
assert_line "$legacy" "ctl_gesture_right=assistant"
assert_line "$legacy" "ctl_anc_gesture_loop=active,aware"
assert_line "$legacy" "ctl_balance=20"
assert_line "$legacy" "ctl_eq=0.00,1.50,-2.00,0.50,3.00"

adaptive=$(PBPCTRL_TEST_ADAPTIVE=1 "$repo/status.sh" --controls)
assert_line "$adaptive" "adaptive_supported=1"
assert_line "$adaptive" "ctl_anc_gesture_loop=active,aware,adaptive"

echo "status controls test passed"
