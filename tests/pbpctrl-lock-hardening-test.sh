#!/bin/sh
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
mkdir "$tmp/bin" "$tmp/runtime" "$tmp/evil"

cat >"$tmp/bin/pbpctrl" <<'EOF'
#!/bin/sh
exit 0
EOF
chmod +x "$tmp/bin/pbpctrl"

export PATH="$tmp/bin:$PATH"
export XDG_RUNTIME_DIR="$tmp/runtime"

mkdir "$tmp/runtime/omarchy-pixelbuds"
ln -s "$tmp/evil/target" "$tmp/runtime/omarchy-pixelbuds/pbpctrl.lock"

if "$repo/pbpctrl-locked.sh" get anc >/dev/null 2>&1; then
  echo "pbpctrl lock helper accepted symlink lock file" >&2
  exit 1
fi

echo "pbpctrl lock hardening test passed"
