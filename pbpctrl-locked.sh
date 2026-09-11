#!/bin/sh
# Omarchy creates one bar-widget instance per monitor. Serialize the RFCOMM
# profile registration across those instances so BlueZ never receives two
# simultaneous pbpctrl sessions for the same vendor UUID.

runtime_dir=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
[ -d "$runtime_dir" ] || {
  echo "pbpctrl lock directory is unavailable: $runtime_dir" >&2
  exit 75
}

uid=$(id -u)

owner_uid() { stat -Lc '%u' "$1" 2>/dev/null; }

verify_private_dir() {
  p=$1
  [ -d "$p" ] || return 1
  [ ! -L "$p" ] || return 1
  [ "$(owner_uid "$p")" = "$uid" ] || return 1
}

verify_regular_file() {
  p=$1
  [ -f "$p" ] || return 1
  [ ! -L "$p" ] || return 1
  [ "$(owner_uid "$p")" = "$uid" ] || return 1
}

verify_private_dir "$runtime_dir" || {
  echo "pbpctrl lock runtime dir must be owned by uid $uid and not a symlink" >&2
  exit 75
}

lock_dir="$runtime_dir/omarchy-pixelbuds"
if [ ! -e "$lock_dir" ]; then
  umask 077
  mkdir "$lock_dir" 2>/dev/null || :
fi
verify_private_dir "$lock_dir" || {
  echo "pbpctrl lock directory is unsafe: $lock_dir" >&2
  exit 75
}
chmod 700 "$lock_dir" 2>/dev/null || {
  echo "pbpctrl lock directory permissions are unsafe: $lock_dir" >&2
  exit 75
}

lock_file="$lock_dir/pbpctrl.lock"
if [ ! -e "$lock_file" ]; then
  umask 077
  : >"$lock_file" 2>/dev/null || :
fi
verify_regular_file "$lock_file" || {
  echo "pbpctrl lock file is unsafe: $lock_file" >&2
  exit 75
}

umask 077
exec 9<>"$lock_file" || exit 75
flock -w 8 9 || {
  echo "timed out waiting for another Pixel Buds control operation" >&2
  exit 75
}
exec pbpctrl "$@"
