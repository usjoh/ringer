#!/bin/bash
# Ringer engine wrapper: run OpenCode under a macOS Seatbelt sandbox.
#
# OpenCode has no OS-level sandbox of its own — its --dangerously-skip-permissions
# flag (required for headless runs) disables ALL of its interactive approval
# prompts. This wrapper supplies the real containment: full network and reads,
# writes confined to the task dir, a per-run scratch/cache dir, and OpenCode's
# own state dirs.
#
# Usage (as a ringer engine bin):
#   opencode-sandboxed.sh <taskdir> [--no-sandbox] <opencode args...>
#
# The first argument is the task directory (pass "{taskdir}" first in
# args_template). "--no-sandbox" as the second argument skips Seatbelt entirely
# — wire it as the engine's full_access_args so ringer's allow_full_access gate
# still applies. macOS only (sandbox-exec); on other platforms only
# --no-sandbox mode works.
set -euo pipefail

TASKDIR="${1:?usage: opencode-sandboxed.sh <taskdir> [--no-sandbox] <args...>}"; shift
SANDBOX=1
if [ "${1:-}" = "--no-sandbox" ]; then SANDBOX=0; shift; fi

# Stagger simultaneous spawns. This was the ONLY defence against the shared
# session DB (see XDG_DATA_HOME below) and never a cure — it only narrowed the
# window in which two workers collide. Now that each sandboxed worker gets a
# private store it is a secondary net, kept because it also spreads the startup
# burst of network and provider calls. Safe to delete once a wide fan-out has
# been observed clean. (Contention observed 2026-07-06 and 2026-07-08.)
# The %02d pad keeps the fraction two digits so the delay is uniform across
# [0.00, 3.99] — a bare $((RANDOM % 100)) yields "3.5" (=3.5s), not "3.05".
sleep "$((RANDOM % 4)).$(printf '%02d' "$((RANDOM % 100))")"

# Resolve opencode without tripping `set -e` (command -v returns nonzero when absent).
if ! OPENCODE_BIN="$(command -v opencode)" || [ -z "$OPENCODE_BIN" ]; then
  echo "opencode-sandboxed.sh: opencode not found on PATH" >&2
  exit 127
fi

if [ "$SANDBOX" = "0" ]; then
  # Deliberately keeps the SHARED session DB. The private-store fix below hangs
  # off the scratch dir and its EXIT trap, and this path `exec`s — the trap
  # would never fire, so the scratch root would leak on every full-access run.
  # Fine for a lone full-access task; do not fan this mode out wide.
  exec "$OPENCODE_BIN" "$@" < /dev/null
fi

if [ ! -x /usr/bin/sandbox-exec ]; then
  echo "opencode-sandboxed.sh: /usr/bin/sandbox-exec not available (macOS only)." >&2
  echo "Use the engine's full-access mode (--no-sandbox) or add your own sandbox." >&2
  exit 1
fi

TASKDIR_REAL="$(cd "$TASKDIR" && pwd -P)"

# Per-run scratch root — becomes both TMPDIR and XDG_CACHE_HOME for OpenCode, so
# we never have to open all of /private/tmp or ~/.cache to the sandboxed agent.
# Resolve to the real path (/var/folders symlinks to /private/var/folders);
# Seatbelt subpath matching needs the canonical path or writes EPERM-crash.
SCRATCH="$(cd "$(mktemp -d -t ringer-opencode-scratch)" && pwd -P)"
PROFILE="$(mktemp -t ringer-opencode-prof)"
cleanup() { rm -rf "$SCRATCH" "$PROFILE"; }
trap cleanup EXIT

# Paths are passed to the profile via sandbox-exec -D parameters, NOT string
# interpolation — a task dir containing quotes/parens/newlines can't inject rules.
cat > "$PROFILE" <<'SBEOF'
(version 1)
(allow default)
(deny file-write*)
(allow file-write*
  (subpath (param "TASKDIR"))
  (subpath (param "SCRATCH"))
  (subpath (param "OC_SHARE"))
  (subpath (param "OC_STATE"))
  (subpath (param "OC_CONFIG"))
  (subpath (param "OC_BASE")))
; /dev is needed for /dev/null, /dev/urandom, etc.; writes there can't create
; persistent files without root, so a few literals are allowed rather than via param.
(allow file-write-data
  (literal "/dev/null")
  (literal "/dev/dtracehelper")
  (literal "/dev/tty"))
SBEOF

export TMPDIR="$SCRATCH"
export XDG_CACHE_HOME="$SCRATCH/cache"
mkdir -p "$XDG_CACHE_HOME"

# Private session store per worker.
#
# OpenCode keeps its session state at $XDG_DATA_HOME/opencode/opencode.db —
# ONE SQLite file, shared by every instance, opened for write and growing
# without bound (145 MB on this machine as of 2026-08-16). Fan several workers
# out at once and the losers of the lock race die before the model emits a
# token:
#
#     Error: Unexpected error
#     database is locked
#
# Ringer records that as an ordinary FAIL, so the scoreboard reads it as the
# model failing when no model ever ran. Every non-Codex model routes through
# this engine, so the damage lands hardest on exactly the cheap tier you fan
# out widest, and worsens as parallelism rises — a surface misstating what
# happened, which is the failure class this project treats as unforgivable.
#
# A private data root per worker removes the contention outright. It lives
# under SCRATCH, so the existing EXIT trap tears it down with everything else,
# and the Seatbelt profile already grants SCRATCH write access.
export XDG_DATA_HOME="$SCRATCH/share"
mkdir -p "$XDG_DATA_HOME/opencode"

# Credentials live INSIDE the directory we just moved, so seed them or the
# worker starts with no provider auth at all. COPY, never symlink: the profile
# denies writes outside SCRATCH and a symlink would resolve straight back to
# the shared file this exists to avoid. -p preserves the 0600 mode.
if [ -f "$HOME/.local/share/opencode/auth.json" ]; then
  cp -p "$HOME/.local/share/opencode/auth.json" "$XDG_DATA_HOME/opencode/auth.json"
fi

# Run as a child (not exec) so the EXIT trap fires and cleans up the profile +
# scratch dir even on the success path; propagate the child's exit status.
set +e
/usr/bin/sandbox-exec \
  -D "TASKDIR=$TASKDIR_REAL" \
  -D "SCRATCH=$SCRATCH" \
  -D "OC_SHARE=$HOME/.local/share/opencode" \
  -D "OC_STATE=$HOME/.local/state/opencode" \
  -D "OC_CONFIG=$HOME/.config/opencode" \
  -D "OC_BASE=$HOME/.opencode" \
  -f "$PROFILE" "$OPENCODE_BIN" "$@" < /dev/null
status=$?
set -e
exit "$status"
