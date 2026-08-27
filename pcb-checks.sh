#!/usr/bin/env bash
# Run the check gates over a project's whole board list. Reads the list on stdin,
# one board per line, each optionally followed by skip=a,b and/or todo=c,d:
#
#   hardware/my-board
#   hardware/my-board/my-module   skip=pinmap
#   hardware/my-old-board         skip=pinmap todo=drc,3d,drift
#
#   pcb-checks.sh [--cmd CMD] [--only NAME] [--extra-checks SCRIPT] < boards.txt
#
#     --cmd           gate to run per board (default: check, the whole set)
#     --only NAME     just this board, and IGNORE its skip/todo. You named it, so
#                     answering "skipped" would defeat the point. This is how you
#                     find out whether a gate has gone green.
#     --extra-checks  project-specific script, run as `<script> <project-dir>`
#                     after the standard gates.
#
# This lives here rather than inline in the workflow so it can be run and tested
# outside CI, which is also what a project's local wrapper calls. One failing board
# never stops the others: every board is reported, then the exit code is the union.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"

cmd=check; only=""; extra=""
while [ $# -gt 0 ]; do
  case "$1" in
    --cmd)          cmd="${2:?--cmd needs a gate}"; shift 2 ;;
    --only)         only="${2:?--only needs a board}"; shift 2 ;;
    --extra-checks) extra="${2:-}"; shift 2 ;;
    *) echo "$0: unknown argument '$1'" >&2; exit 2 ;;
  esac
done

# Validate the list before running anything: a typo in a gate name must be a hard
# error, not a gate that silently stops being enforced.
boards=$(python3 "$here/scripts/boards.py" --stdin --format lines) || exit 2

rc=0; ran=0
while IFS='|' read -r dir skip todo; do
  [ -n "${dir:-}" ] || continue
  name=$(basename "$dir")
  if [ -n "$only" ] && [ "$name" != "$(basename "$only")" ]; then continue; fi
  ran=$((ran + 1))
  echo "### =================================================="
  echo "### $dir"
  echo "### =================================================="
  if [ -n "$only" ]; then
    "$here/pcb-release.sh" "$dir" "$cmd" || rc=1
  else
    "$here/pcb-release.sh" "$dir" "$cmd" --skip="$skip" --todo="$todo" || rc=1
  fi
  if [ -n "$extra" ]; then "$extra" "$dir" || rc=1; fi
done <<EOF
$boards
EOF

if [ "$ran" -eq 0 ]; then
  echo "$0: '$only' matched no board in the list" >&2; exit 2
fi
. "$here/colors.sh"
if [ $rc -eq 0 ]; then v="${C_OK}PASS${C_OFF}"; else v="${C_ERR}FAIL${C_OFF}"; fi
echo "=== $cmd: $ran board(s) -> $v ==="
exit $rc
