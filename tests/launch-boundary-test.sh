#!/bin/sh
# HANCORE Layer-2 launch boundary: trusted identities, closed env, no ambient PATH.
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM

fail() {
  echo "$1" >&2
  exit 1
}

qml="$repo/Panel.qml"
helper="$repo/pbpctrl-locked.sh"
status="$repo/status.sh"
cache="$repo/casecache.py"
model="$repo/Model.js"

# --- QML contract (Underpants / Clanky) ---
grep -q 'clearEnvironment: true' "$qml" || fail "Panel.qml missing clearEnvironment"
grep -q 'PATH: root.trustedPath' "$qml" || grep -q 'PATH: "/usr/bin:/bin"' "$qml" || fail "Panel.qml missing closed PATH"
grep -q 'function launchEnvironment()' "$qml" || fail "Panel.qml missing launchEnvironment"
grep -q 'readonly property string shBin: "/usr/bin/sh"' "$qml" || fail "Panel.qml missing /usr/bin/sh"
grep -q 'readonly property string timeoutBin: "/usr/bin/timeout"' "$qml" || fail "Panel.qml missing /usr/bin/timeout"
grep -q 'readonly property string gdbusBin: "/usr/bin/gdbus"' "$qml" || fail "Panel.qml missing /usr/bin/gdbus"
grep -q 'readonly property string python3Bin: "/usr/bin/python3"' "$qml" || fail "Panel.qml missing /usr/bin/python3"
grep -q 'readonly property string wlCopyBin: "/usr/bin/wl-copy"' "$qml" || fail "Panel.qml missing /usr/bin/wl-copy"
grep -q 'readonly property string omarchyShellBin: "/usr/bin/omarchy-shell"' "$qml" || fail "Panel.qml missing /usr/bin/omarchy-shell"
grep -q 'root.python3Bin, "-I"' "$qml" || fail "Panel.qml must launch the lock helper with python3 -I"

# Ambient-PATH tokens must not appear as command argv literals.
! grep -q '\["sh"' "$qml" || fail "Panel.qml still launches sh via ambient PATH"
! grep -q '\["timeout"' "$qml" || fail "Panel.qml still launches timeout via ambient PATH"
! grep -q '\["gdbus"' "$qml" || fail "Panel.qml still launches gdbus via ambient PATH"
! grep -q '\["wl-copy"' "$qml" || fail "Panel.qml still launches wl-copy via ambient PATH"
! grep -q '\["omarchy-shell"' "$qml" || fail "Panel.qml still launches omarchy-shell via ambient PATH"
! grep -q 'execDetached' "$qml" || fail "Panel.qml still uses execDetached (no closed env)"
! grep -q 'Quickshell.env("PATH")' "$qml" || fail "Panel.qml must not inherit ambient PATH"
for needle in PYTHONPATH PYTHONHOME PYTHONINSPECT PYTHONSTARTUP LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT; do
  ! grep -q "$needle" "$qml" || fail "Panel.qml forwards $needle"
done
! grep -q '"PIXELBUDS_TRUSTED_PATH"' "$qml" || fail "Panel.qml must not forward PIXELBUDS_TRUSTED_PATH"

# QML-side StdioCollector second ceiling.
grep -q 'function clip(' "$model" || fail "Model.js missing clip() second ceiling"
grep -q 'statusStdoutCeiling' "$qml" || fail "Panel.qml missing statusStdoutCeiling"
grep -q 'Model.clip(raw, root.statusStdoutCeiling)' "$qml" || fail "applyStatus must clip collector text"
grep -q 'Model.clip(raw, root.controlsStdoutCeiling)' "$qml" || fail "applyControls must clip collector text"

# --- Shebangs / exec identity ---
# Isolated interpreter only. A plain /usr/bin/python3 shebang is a regression.
case "$(head -n1 "$helper")" in
  "#!/usr/bin/python3 -I") ;;
  *) fail "pbpctrl-locked.sh shebang must be isolated /usr/bin/python3 -I" ;;
esac
case "$(head -n1 "$cache")" in
  "#!/usr/bin/python3 -I") ;;
  *) fail "casecache.py shebang must be isolated /usr/bin/python3 -I" ;;
esac
! grep -q 'os.execvp' "$helper" || fail "pbpctrl-locked.sh still uses execvp"
grep -q 'os.execve' "$helper" || fail "pbpctrl-locked.sh must execve"
! grep -n '^[^#]*command -v' "$status" || fail "status.sh still uses command -v"
grep -q 'PATH=/usr/bin:/bin' "$status" || fail "status.sh must close ambient PATH"

# --- Shadowed PATH must not win ---
mkdir "$tmp/shadow" "$tmp/bin" "$tmp/runtime" "$tmp/state"
chmod 700 "$tmp/runtime"
for name in pbpctrl bluetoothctl timeout head sh python3 gdbus wl-copy omarchy-shell; do
  cat >"$tmp/shadow/$name" <<'EOF'
#!/bin/sh
echo SHADOWED
exit 42
EOF
  chmod +x "$tmp/shadow/$name"
done

export PATH="$tmp/shadow:/usr/bin:/bin"
unset PIXELBUDS_TRUSTED_PATH || true
export XDG_RUNTIME_DIR="$tmp/runtime"
export XDG_STATE_HOME="$tmp/state"

# Helper: no extra trusted dir → refuse shadowed pbpctrl.
if "$helper" get anc >"$tmp/helper.out" 2>"$tmp/helper.err"; then
  fail "lock helper accepted shadowed PATH pbpctrl"
fi
! grep -q SHADOWED "$tmp/helper.out" "$tmp/helper.err" || fail "lock helper executed shadowed pbpctrl"
grep -q 'pbpctrl not found' "$tmp/helper.err" || fail "lock helper did not fail closed without trusted pbpctrl"

# status.sh: shadowed bluetoothctl/pbpctrl on PATH must not run.
status_out=$("$status" --controls 2>"$tmp/status.err" || true)
! printf '%s\n' "$status_out" | grep -q SHADOWED || fail "status.sh executed a shadowed tool"
! grep -q SHADOWED "$tmp/status.err" || fail "status.sh stderr came from a shadowed tool"
# Without a trusted bluetoothctl this is a clean disconnect, not a shadow hit.
printf '%s\n' "$status_out" | grep -Fxq 'connected=0' || fail "status.sh without trusted bluetoothctl should report connected=0"

# Trusted extra dir still selects the test stub (existing functional tests rely on this).
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
echo active
EOF
chmod +x "$tmp/bin/bluetoothctl" "$tmp/bin/pbpctrl"
export PIXELBUDS_TRUSTED_PATH="$tmp/bin"
trusted_out=$(PIXELBUDS_TRUSTED_PATH="$tmp/bin" "$status" 2>/dev/null || true)
printf '%s\n' "$trusted_out" | grep -Fxq 'connected=1' || fail "PIXELBUDS_TRUSTED_PATH stubs were not used"
! printf '%s\n' "$trusted_out" | grep -q SHADOWED || fail "trusted stub leaked SHADOWED"

# Relative / parent-dir extra paths are refused.
if PIXELBUDS_TRUSTED_PATH="../evil" "$helper" get anc >"$tmp/rel.out" 2>"$tmp/rel.err"; then
  fail "lock helper accepted a relative PIXELBUDS_TRUSTED_PATH"
fi
! grep -q SHADOWED "$tmp/rel.out" "$tmp/rel.err" || fail "relative trusted path executed a shadow"

echo "launch boundary test passed"
