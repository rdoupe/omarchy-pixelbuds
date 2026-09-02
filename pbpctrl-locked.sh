#!/bin/sh
# Omarchy creates one bar-widget instance per monitor. Serialize the RFCOMM
# profile registration across those instances so BlueZ never receives two
# simultaneous pbpctrl sessions for the same vendor UUID.

runtime_dir=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
[ -d "$runtime_dir" ] || {
  echo "pbpctrl lock directory is unavailable: $runtime_dir" >&2
  exit 75
}

umask 077
exec 9>"$runtime_dir/omarchy-pixelbuds-pbpctrl.lock" || exit 75
flock -w 8 9 || {
  echo "timed out waiting for another Pixel Buds control operation" >&2
  exit 75
}
exec pbpctrl "$@"
