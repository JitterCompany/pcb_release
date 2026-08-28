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
#     --model-dir     NAME=PATH for a 3D path variable this repo defines itself,
#                     repeatable. Those variables live in each developer's KiCad
#                     preferences, so without this nothing tells CI where the repo's
#                     own model library is and every such model fails to resolve.
#
# This lives here rather than inline in the workflow so it can be run and tested
# outside CI, which is also what a project's local wrapper calls. One failing board
# never stops the others: every board is reported, then the exit code is the union.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/colors.sh"

# Every failure gets a verdict line. A path that sets rc without one produces a run
# where each stage says PASS and the total says FAIL, with nothing to point at.
verdict() {                            # $1 = label, $2 = rc
  if [ "$2" -eq 0 ]; then printf '%s  PASS%s  %s\n' "$C_OK" "$C_OFF" "$1"
  else                    printf '%s  FAIL%s  %s\n' "$C_ERR" "$C_OFF" "$1"; fi
}

cmd=check; only=""; extra=""; model_args=""
while [ $# -gt 0 ]; do
  case "$1" in
    --cmd)          cmd="${2:?--cmd needs a gate}"; shift 2 ;;
    --only)         only="${2:?--only needs a board}"; shift 2 ;;
    --extra-checks) extra="${2:-}"; shift 2 ;;
    --model-dir)    [ -n "${2:-}" ] && model_args="$model_args --model-dir=$2"; shift 2 ;;
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
  # A board with EVERY gate spoken for runs nothing, so say it once instead of
  # repeating a warning per gate. A repo that has just started listing its boards is
  # mostly this, and 5 warnings x N boards drowns the boards that are actually checked.
  if [ -z "$only" ]; then
    covered=1
    for g in erc drc 3d drift pinmap; do
      case ",$skip,$todo," in *",$g,"*) ;; *) covered=0 ;; esac
    done
    if [ "$covered" -eq 1 ]; then
      if [ -n "$todo" ]; then
        echo "::warning title=$(basename "$dir") is not checked yet::No gate is enforced for $dir. Fix its findings and drop gates from its todo= in the CI board list." | annot_render
      else
        echo "### $dir: no gate applies to this board -- skipped"
      fi
      continue
    fi
  fi
  ran=$((ran + 1))
  echo "### =================================================="
  echo "### $dir"
  echo "### =================================================="
  if [ -n "$only" ]; then
    "$here/pcb-release.sh" "$dir" "$cmd" $model_args || rc=1
  else
    "$here/pcb-release.sh" "$dir" "$cmd" --skip="$skip" --todo="$todo" $model_args || rc=1
  fi
  if [ -n "$extra" ]; then
    # A project's own script is a gate like any other, so it reports like one.
    "$extra" "$dir"; k=$?
    [ $k -eq 0 ] || { verdict "$(basename "$dir") / $(basename "$extra")" "$k"; rc=1; }
  fi
done <<EOF
$boards
EOF

if [ "$ran" -eq 0 ]; then
  echo "$0: '$only' matched no board in the list" >&2; exit 2
fi
if [ $rc -eq 0 ]; then v="${C_OK}PASS${C_OFF}"; else v="${C_ERR}FAIL${C_OFF}"; fi
echo "=== $cmd: $ran board(s) -> $v ==="
exit $rc
