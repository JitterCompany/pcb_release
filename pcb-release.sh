#!/usr/bin/env bash
# pcb_release — KiCad design checks + manufacturing release. THE single entry
# point; everything else lives in scripts/ and is called from here.
#
# Automated (config-driven) workflow — needs a <project>/release.toml:
#   pcb-release.sh PROJECT_DIR check       ERC+schema, DRC+pos>=BOM, release-spec drift
#   pcb-release.sh PROJECT_DIR release     fab zip (production/ -> partner) + customer set
#                                          (STEP/PDF/layout PDF/renders/iBOM -> customer/)
#   pcb-release.sh PROJECT_DIR erc|drc|3d|all  just that check gate (used by CI jobs)
#   pcb-release.sh PROJECT_DIR drift       is the committed release_spec.toml still true?
#   pcb-release.sh PROJECT_DIR bom         BOM_<board>.csv beside the project, for a
#                                          pre-release read-through. Not a gate, and
#                                          needs no release.toml.
#   pcb-release.sh PROJECT_DIR pinmap | pinmap-check | pinmap-drift
#                                          MCU pin map: regenerate / validate / drift-check
#                                          ERRORS if <proj>/pinmap.config.toml is absent
#                                          (composite 'check' skips it instead)
# Env: KICAD_IBOM_DIR  InteractiveHtmlBom checkout (else it is fetched automatically).
#      CHECK_OUT      dir for the ERC/DRC JSON reports (default: a fresh tmp dir).
#                     LOCAL DEBUGGING ONLY -- CI never sets it; findings surface as
#                     GitHub annotations instead, so nothing needs downloading.
#
# Manual (interactive, legacy) workflow — no config, prompts you step by step:
#   pcb-release.sh PROJECT_DIR manual      guided export + zip (scripts/release_pcb.py)
#
# Env: KICAD_IGNORE_TYPES  comma-sep ERC/DRC 'type' keys to suppress as CI-env
#                          noise (headless run has no library table).
#      KICAD_3D_LINT_ARGS  extra flags for the 3D gate (scripts/model3d_lint.py),
#                          e.g. -D for a private model library, or --no-file-check
#                          when the model files aren't fetched in this job.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; S="$here/scripts"

proj="${1:?usage: $0 PROJECT_DIR <check|release|erc|drc|3d|all|drift|bom|pinmap[-check|-drift]|manual> [--strict]}"; shift || true
cmd="${1:-check}"; shift || true
# --strict (warnings also fail) is MANUAL-ONLY and deliberately unreachable from CI:
# no workflow and no pcb.sh path passes it. A release gates EXACTLY like a normal
# push, so that observing a green CI run predicts a green release. Do not wire this
# into CI -- to make a warning gate, raise its severity in the KiCad GUI, where the
# decision travels with the project and is reviewed in the diff.
strict=""; skip_gates=""; todo_gates=""; model_dirs=""
for a in "$@"; do
  case "$a" in
    --strict)      strict="--strict" ;;
    --skip=*)      skip_gates="${a#--skip=}" ;;
    --todo=*)      todo_gates="${a#--todo=}" ;;
    # Repeatable NAME=PATH. A repo whose 3D models live in its own library needs to
    # say where: that path variable is set in each developer's KiCad preferences, so
    # nothing in the repo tells CI about it and every model silently fails to resolve.
    --model-dir=*) model_dirs="$model_dirs ${a#--model-dir=}" ;;
  esac
done

# Manual workflow runs IN the project dir (it globs *.kicad_pro there, writes production/).
if [ "$cmd" = "manual" ]; then cd "$proj" && exec python3 "$S/release_pcb.py"; fi

pro=$(ls "$proj"/*.kicad_pro 2>/dev/null | head -1)
[ -n "$pro" ] || { echo "pcb-release: no .kicad_pro in '$proj'" >&2; exit 2; }
stem=$(basename "$pro" .kicad_pro); sch="$proj/$stem.kicad_sch"; pcb="$proj/$stem.kicad_pcb"
# A headless kicad-cli has no symbol/footprint library table, so it flags every
# library as "not included" on EVERY project. That is environment noise, not a
# design finding -- suppress it by default so a new project needs no config at
# all; set KICAD_IGNORE_TYPES to override (empty string = suppress nothing).
ignore="${KICAD_IGNORE_TYPES-lib_symbol_issues,footprint_link_issues,lib_footprint_issues}"
# BSD/macOS mktemp REQUIRES a template; GNU accepts one. Always pass one, or this
# dies on a Mac with a bare usage error.
TMPL="${TMPDIR:-/tmp}/pcb-release.XXXXXX"
out="${CHECK_OUT:-$(mktemp -d "$TMPL")}"; mkdir -p "$out"
# 3D lint flags. KICAD_3D_LINT_ARGS is word-split on purpose: it carries flags. Each
# --model-dir becomes a -D NAME=<absolute path>, absolute because the lint resolves
# model files by path and is not run from a fixed directory.
m3d="${KICAD_3D_LINT_ARGS:-}"
for md in $model_dirs; do
  md_name="${md%%=*}"; md_path="${md#*=}"
  case "$md_path" in
    /*) ;;
    *)  if [ -d "$md_path" ]; then md_path="$(cd "$md_path" && pwd)"
        else echo "::warning::[3d] --model-dir $md_name: no such directory '$md_path'"; fi ;;
  esac
  m3d="$m3d -D $md_name=$md_path"
done
rc=0
board=$(basename "$proj")

# ---- per-board gate policy ------------------------------------------------
# EVERY gate is enforced unless this board says otherwise, so a board cannot go
# silently unchecked and a gate cannot be forgotten, only exempted on purpose.
#   --skip=  structurally impossible here (no MCU means no pin map) -> silent
#   --todo=  applies, not green yet -> warn every run, do not fail
# `pinmap` in either list covers both pinmap gates: they are one concern to a
# board owner, and naming them separately only invites listing one of the two.
gate_listed() {                        # $1 = comma list, $2 = gate
  local e
  for e in ${1//,/ }; do
    [ "$e" = "$2" ] && return 0
    case "$2" in "$e"-*) return 0 ;; esac
  done
  return 1
}

# Run a gate subject to that policy. $1 = gate name, rest = what to run.
gate() {
  local g="$1"; shift
  if gate_listed "$skip_gates" "$g"; then
    echo "### $board: '$g' does not apply to this board -- skipped"
    return 0
  fi
  if gate_listed "$todo_gates" "$g"; then
    echo "::warning title=$g not enforced for $board::The '$g' gate is not green for $board yet and is NOT gating CI. Fix the findings, then drop '$g' from that board's todo= in the CI workflow." | annot_render
    return 0
  fi
  "$@"
}

# ---- console -------------------------------------------------------------
. "$here/colors.sh"

# EVERY stage ends with exactly one of these and nothing else claims a verdict,
# so a scroll-back reads as one PASS/FAIL column per board.
stage_result() {                       # $1 = stage label, $2 = rc
  if [ "$2" -eq 0 ]; then printf '%s  PASS%s  %s / %s\n' "$C_OK" "$C_OFF" "$board" "$1"
  else                    printf '%s  FAIL%s  %s / %s\n' "$C_ERR" "$C_OFF" "$board" "$1"; fi
}

# Run a stage QUIETLY: its chatter is shown only if it fails (or PCB_VERBOSE=1).
# GitHub annotations (::error/::warning/::notice) always pass through -- those are
# the findings, not chatter, so a green run stays quiet without hiding anything
# that matters. This is why kicad-cli's "Found N violations" no longer confuses:
# that count includes warnings and deliberately-ignored types, so on a PASS it
# only ever contradicted the verdict.
stage() {
  local label="$1"; shift
  local log; log=$(mktemp "$TMPL")
  "$@" >"$log" 2>&1; local k=$?
  local ci=0;   [ "${GITHUB_ACTIONS:-}" = "true" ] && ci=1
  local show=0; { [ $k -ne 0 ] || [ -n "${PCB_VERBOSE:-}" ]; } && show=1
  # ::error/::warning/::notice ARE the GitHub checks UI, so under Actions they
  # always go out -- a non-gating warning still belongs on the PR. In a terminal
  # they are just noise on a green run, so there they are buffered like everything
  # else and only surface when the stage actually failed. Net effect locally:
  # a pass is ONE line; a fail shows warnings + errors + detail, then FAIL.
  if [ $ci -eq 1 ] || [ $show -eq 1 ]; then
    grep -E '^::(error|warning|notice)' "$log" | annot_render || true
  fi
  if [ $show -eq 1 ]; then
    sed -E '/^::(error|warning|notice)/d' "$log" | sed "s/^/${C_DIM}    /;s/$/${C_OFF}/"
  fi
  rm -f "$log"
  stage_result "$label" "$k"
  [ $k -eq 0 ] || rc=1
  return $k
}

# kicad-cli check -> all-severity JSON -> check_report.py policy (errors gate;
# warnings only with --strict; KiCad-excluded never gate; --ignore-type suppressed).
kicad_check() {
  local label="$1"; shift; local rep="$out/$(printf %s "$label" | tr '[:upper:]' '[:lower:]').json"
  "$@" --severity-all --format json --output "$rep" >/dev/null 2>&1; local k=$?
  [ -s "$rep" ] || { echo "::error::[$label] kicad-cli produced no report (exit $k)"; return 1; }
  python3 "$S/check_report.py" --label "$label" $strict --ignore-type "$ignore" "$rep"
}
gate_erc() { stage ERC kicad_check ERC kicad-cli sch erc "$sch"
             stage "SCHEMA (dnp-lint)" python3 "$S/dnp_lint.py" "$sch"; }
gate_drc() { stage DRC kicad_check DRC kicad-cli pcb drc --schematic-parity "$pcb"
             stage "SCHEMA (pos>=bom)" python3 "$S/release_ci.py" "$proj" --mode pnp; }
# 3D: cheap (~1s, pure python) and it runs BEFORE any export, because kicad-cli
# reports an unresolvable model on stdout and still exits 0 -- so a STEP with
# parts silently missing looks exactly like a good one. Customers fit-check
# against that STEP, so it gates.
gate_3d()  { stage "3D MODELS" python3 "$S/model3d_lint.py" "$pcb" --require-model $m3d; }
# The model fetch is part of the 3D gate, not a separate step: skipping the gate
# must also skip the fetch, or a board with skip=3d still pays for a git clone.
run_3d()   { setup_models; gate_3d; }
# Both pinmap gates behind ONE policy decision, so a board that skips pinmap says so
# once rather than once per sub-gate.
run_pinmap() { pinmap check; pinmap drift; }

# Point the shared 3D model library at a real directory, exported so BOTH
# kicad-cli and the linter resolve the same files. The STOCK KiCad library is
# deliberately NOT handled here -- it is an environment prerequisite
# (kicad-packages3d / the `-full` image), not a package manager to re-implement.
# NOTE: not wrapped in stage() -- this one must run in the CURRENT shell so its
# exports survive, and its stdout is the KEY=VALUE payload, not chatter. Same quiet
# policy applied by hand to its stderr progress.
setup_models() {
  local out line err k
  err=$(mktemp "$TMPL")
  out=$(python3 "$S/model_libs.py" "$proj" 2>"$err"); k=$?   # progress/errors -> stderr
  # Emit a verdict line when this fails. Without one, rc was set here while every stage
  # printed PASS, so the run ended "all PASS -> FAIL" with nothing to point at.
  if [ $k -ne 0 ]; then stage_result "3D MODEL FETCH" "$k"; rc=1; fi
  if [ "${GITHUB_ACTIONS:-}" = "true" ] || [ $k -ne 0 ] || [ -n "${PCB_VERBOSE:-}" ]; then
    grep -E '^::(error|warning|notice)' "$err" | annot_render || true
  fi
  if [ $k -ne 0 ] || [ -n "${PCB_VERBOSE:-}" ]; then
    sed -E '/^::(error|warning|notice)/d' "$err" | sed "s/^/${C_DIM}    /;s/$/${C_OFF}/"
  fi
  rm -f "$err"
  while IFS= read -r line; do
    [ -n "$line" ] && export "$line"
  done <<<"$out"
}

# Pin map (MCU boards only). Config is <proj>/pinmap.config.toml -- deliberately
# NOT release.toml: a pin map is a schematic-stage artifact, usually settled long
# before layout.
#   $1 = "" | check | drift
#   $2 = "optional" -> a board with no pin map is fine (composite gates)
# Asking for it EXPLICITLY (pinmap-check in CI, which only runs when the caller
# opted in with pinmap:true) and finding no config is a misconfiguration, not a
# passive board -- fail, or the job would pass green having checked nothing.
pinmap() {
  local conf="$proj/pinmap.config.toml"
  if [ ! -f "$conf" ]; then
    [ "${2:-}" = optional ] && { echo "[pinmap] no $conf -- skipped (board has no pin map)"; return 0; }
    # The pin map is enforced by default, so a missing config is a real failure:
    # either the board has an MCU and nobody made one, or it has none and should say
    # skip=pinmap. Report it with a verdict line like every other gate, or the run
    # ends "every stage PASS -> FAIL" with nothing to point at.
    echo "::error::[pinmap] no $conf. Create one (see scripts/generate_pinmap.py" \
         "load_config), or give this board skip=pinmap in the CI board list if it has" \
         "no MCU." | annot_render
    stage_result "PINMAP ${1:-generate}" 1
    rc=1; return 1
  fi
  stage "PINMAP ${1:-generate}" python3 "$S/generate_pinmap.py" --config "$conf" ${1:+--$1} "$sch"
}

# Policy applies exactly when the caller supplies it. Passing no --skip/--todo (the
# default, and what `pcb-release.sh X erc` on its own does) runs the gate unchanged,
# so naming a gate directly always runs it. A caller that DOES pass a board's policy
# gets it honoured for single gates too, which is what "run this gate the way CI
# would" means.
case "$cmd" in
  erc)     gate erc gate_erc ;;
  drc)     gate drc gate_drc ;;
  pinmap)       gate pinmap pinmap ;;
  pinmap-check) gate pinmap pinmap check ;;
  pinmap-drift) gate pinmap pinmap drift ;;
  3d)      gate 3d run_3d ;;
  all)     gate erc gate_erc; gate drc gate_drc
           gate 3d run_3d ;;
  # Not a gate, so no gate() wrapper: no board exempts itself from an export, and
  # skip=/todo= have nothing to say about one. stage() is wrong here too -- it buffers
  # a passing stage, and the path is the whole point of the command.
  bom)     python3 "$S/release_ci.py" "$proj" --mode bom; k=$?
           stage_result "BOM EXPORT" $k; [ $k -eq 0 ] || rc=1 ;;
  drift)   gate drift stage "RELEASE SPEC DRIFT" \
                python3 "$S/release_ci.py" "$proj" --mode drift ;;
  # `check` IS the CI gate set: every gate this board is held to, in one pass.
  check)   gate erc gate_erc; gate drc gate_drc; gate 3d run_3d
           gate pinmap run_pinmap
           gate drift stage "RELEASE SPEC DRIFT" \
                python3 "$S/release_ci.py" "$proj" --mode drift ;;
  release) gate erc gate_erc; gate drc gate_drc; gate 3d run_3d
           gate pinmap pinmap drift    # release: drift only, the map must be current
           # one pass: fab zip (PCBA partner) + customer set (STEP/PDF/renders/iBOM)
           stage "RELEASE BUILD" python3 "$S/release_ci.py" "$proj" --mode build ;;
  *) echo "pcb-release: unknown command '$cmd'" >&2; exit 2 ;;
esac
[ -n "${PCB_VERBOSE:-}" ] && echo "reports in: $out"
exit $rc
