#!/bin/sh
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
helper="$repo/pbpctrl-locked.sh"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
mkdir "$tmp/bin" "$tmp/runtime" "$tmp/evil"
chmod 700 "$tmp/runtime"

cat >"$tmp/bin/pbpctrl" <<'EOF'
#!/bin/sh
exit 0
EOF
chmod +x "$tmp/bin/pbpctrl"

export PIXELBUDS_TRUSTED_PATH="$tmp/bin"
export PATH="/usr/bin:/bin"
export XDG_RUNTIME_DIR="$tmp/runtime"

fail() {
  echo "$1" >&2
  exit 1
}

# Source contract: atomic no-follow descriptor open, never path-check-then-open.
grep -q 'O_NOFOLLOW' "$helper" || fail "lock helper missing O_NOFOLLOW"
grep -q 'O_EXCL' "$helper" || fail "lock helper missing O_EXCL"
grep -q 'O_DIRECTORY' "$helper" || fail "lock helper missing O_DIRECTORY"
grep -q 'st_nlink' "$helper" || fail "lock helper missing nlink check on opened fd"
grep -q 'O_CREAT' "$helper" || fail "lock helper missing O_CREAT"
# Reopen must not truncate; create must not path-check then redirect.
if grep -q 'O_TRUNC' "$helper"; then
  fail "lock helper must not truncate the lock file"
fi
if grep -E 'exec 9<>|: >"\$lock_file"|verify_regular_file' "$helper" >/dev/null; then
  fail "lock helper still checks a pathname and later opens it"
fi
if grep -q 'os.execvp' "$helper"; then
  fail "lock helper still looks up pbpctrl on ambient PATH"
fi
grep -q 'os.execve' "$helper" || fail "lock helper must execve a trusted absolute pbpctrl"
case "$(head -n1 "$helper")" in
  "#!/usr/bin/python3 -I") ;;
  *) fail "lock helper shebang must be isolated /usr/bin/python3 -I" ;;
esac

# Happy path: create the private dir + lock via descriptor-safe open.
"$helper" get anc >/dev/null
lock_dir="$tmp/runtime/omarchy-pixelbuds"
lock_file="$lock_dir/pbpctrl.lock"
[ -d "$lock_dir" ] || fail "lock directory was not created"
[ ! -L "$lock_dir" ] || fail "lock directory is a symlink"
[ -f "$lock_file" ] || fail "lock file was not created"
[ ! -L "$lock_file" ] || fail "lock file is a symlink"
[ "$(stat -Lc '%u %a' "$lock_dir")" = "$(id -u) 700" ] || fail "lock directory mode/owner unsafe"
[ "$(stat -Lc '%u %a %h' "$lock_file")" = "$(id -u) 600 1" ] || fail "lock file mode/owner/nlink unsafe"

# Reopen must not truncate existing contents.
printf 'keep-me\n' >"$lock_file"
chmod 600 "$lock_file"
"$helper" get anc >/dev/null
[ "$(cat "$lock_file")" = "keep-me" ] || fail "reopen truncated the lock file"

# Symlink lock file: refuse and do not write through to the victim.
rm -f "$lock_file"
printf 'secret\n' >"$tmp/evil/target"
ln -s "$tmp/evil/target" "$lock_file"
if "$helper" get anc >/dev/null 2>"$tmp/symlink.err"; then
  fail "pbpctrl lock helper accepted symlink lock file"
fi
[ "$(cat "$tmp/evil/target")" = "secret" ] || fail "symlink lock open truncated the victim"
rm -f "$lock_file"

# Parent directory symlink: refuse (O_NOFOLLOW|O_DIRECTORY on the subdir).
rm -rf "$lock_dir"
mkdir "$tmp/evil/plugin"
ln -s "$tmp/evil/plugin" "$lock_dir"
if "$helper" get anc >/dev/null 2>"$tmp/parent.err"; then
  fail "pbpctrl lock helper accepted symlink lock directory"
fi
[ ! -e "$tmp/evil/plugin/pbpctrl.lock" ] || fail "parent symlink was followed to create a lock"
rm -f "$lock_dir"

# Runtime dir symlink: refuse.
rm -rf "$tmp/runtime"
mkdir "$tmp/real-runtime"
ln -s "$tmp/real-runtime" "$tmp/runtime"
if "$helper" get anc >/dev/null 2>"$tmp/runtime.err"; then
  fail "pbpctrl lock helper followed a symlink XDG_RUNTIME_DIR"
fi
[ ! -e "$tmp/real-runtime/omarchy-pixelbuds" ] || fail "runtime symlink was followed"
rm -f "$tmp/runtime"
mkdir "$tmp/runtime"
chmod 700 "$tmp/runtime"

# Hard-linked lock file: refuse (nlink != 1).
mkdir "$lock_dir"
: >"$lock_file"
chmod 600 "$lock_file"
ln "$lock_file" "$tmp/evil/hard"
if "$helper" get anc >/dev/null 2>"$tmp/nlink.err"; then
  fail "pbpctrl lock helper accepted a hard-linked lock file"
fi
rm -f "$tmp/evil/hard" "$lock_file"

# Directory planted as the lock name: refuse.
mkdir "$lock_file"
if "$helper" get anc >/dev/null 2>"$tmp/dir.err"; then
  fail "pbpctrl lock helper accepted a directory as the lock file"
fi
rmdir "$lock_file"

# FIFO planted as the lock name: refuse, and do not stall.
mkfifo "$lock_file"
if "$helper" get anc >/dev/null 2>"$tmp/fifo.err"; then
  fail "pbpctrl lock helper accepted a FIFO as the lock file"
fi
rm -f "$lock_file"

# Group-accessible lock file: refuse.
: >"$lock_file"
chmod 660 "$lock_file"
if "$helper" get anc >/dev/null 2>"$tmp/mode.err"; then
  fail "pbpctrl lock helper accepted a group-accessible lock file"
fi
rm -f "$lock_file"

echo "pbpctrl lock hardening test passed"
