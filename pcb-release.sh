#!/usr/bin/env bash
# pcb_release — KiCad design checks + manufacturing release. THE single entry
# point; everything else lives in scripts/ and is called from here.
#
# Automated (config-driven) workflow — needs a <project>/release.toml:
#   pcb-release.sh PROJECT_DIR check       ERC+schema, DRC+pos>=BOM, release-spec drift
#   pcb-release.sh PROJECT_DIR release     fab zip (production/ -> partner) + customer set
#   pcb-release.sh PROJECT_DIR docs        customer set only: STEP/PDF/renders/iBOM -> customer/
#   pcb-release.sh PROJECT_DIR erc|drc|3d|all  just that check gate (used by CI jobs)
#   pcb-release.sh PROJECT_DIR pnp|drift   single checks
# Env (docs): KICAD_IBOM_DIR = InteractiveHtmlBom dir, to include an interactive BOM.
#
# Manual (interactive, legacy) workflow — no config, prompts you step by step:
#   pcb-release.sh PROJECT_DIR manual      guided export + zip (scripts/release_pcb.py)
#
# Env: KICAD_IGNORE_TYPES  comma-sep ERC/DRC 'type' keys to suppress as CI-env
#                          noise (headless run has no library table).
#      CHECK_OUT           dir for the JSON reports (default: fresh tmp dir).
#      KICAD_3D_LINT_ARGS  extra flags for the 3D gate (scripts/model3d_lint.py),
#                          e.g. -D for a private model library, or --no-file-check
#                          when the model files aren't fetched in this job.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; S="$here/scripts"

proj="${1:?usage: $0 PROJECT_DIR <check|release|docs|erc|drc|all|pnp|drift|manual> [--strict]}"; shift || true
cmd="${1:-check}"; shift || true
strict=""; for a in "$@"; do [ "$a" = "--strict" ] && strict="--strict"; done

# Manual workflow runs IN the project dir (it globs *.kicad_pro there, writes production/).
if [ "$cmd" = "manual" ]; then cd "$proj" && exec python3 "$S/release_pcb.py"; fi

pro=$(ls "$proj"/*.kicad_pro 2>/dev/null | head -1)
[ -n "$pro" ] || { echo "pcb-release: no .kicad_pro in '$proj'" >&2; exit 2; }
stem=$(basename "$pro" .kicad_pro); sch="$proj/$stem.kicad_sch"; pcb="$proj/$stem.kicad_pcb"
ignore="${KICAD_IGNORE_TYPES:-}"; out="${CHECK_OUT:-$(mktemp -d)}"; mkdir -p "$out"
m3d="${KICAD_3D_LINT_ARGS:-}"                  # word-split on purpose: it carries flags
rc=0

# kicad-cli check -> all-severity JSON -> check_report.py policy (errors gate;
# warnings only with --strict; KiCad-excluded never gate; --ignore-type suppressed).
kicad_check() {
  local label="$1"; shift; local rep="$out/${label,,}.json"
  echo "== $label =="; set +e; "$@" --severity-all --format json --output "$rep"; local k=$?; set -e
  [ -s "$rep" ] || { echo "::error::[$label] kicad-cli produced no report (exit $k)"; rc=1; return; }
  python3 "$S/check_report.py" --label "$label" $strict --ignore-type "$ignore" "$rep" || rc=1
}
gate_erc() { kicad_check ERC kicad-cli sch erc "$sch"; echo "== SCHEMA (dnp-lint) =="; python3 "$S/dnp_lint.py" "$sch" || rc=1; }
gate_drc() { kicad_check DRC kicad-cli pcb drc --schematic-parity "$pcb"; echo "== SCHEMA (pos>=bom) =="; python3 "$S/release_ci.py" "$proj" --mode pnp || rc=1; }
# 3D: cheap (~1s, pure python) and it runs BEFORE any export, because kicad-cli
# reports an unresolvable model on stdout and still exits 0 -- so a STEP with
# parts silently missing looks exactly like a good one. Customers fit-check
# against that STEP, so it gates.
gate_3d()  { echo "== 3D MODELS =="; python3 "$S/model3d_lint.py" "$pcb" --require-model $m3d || rc=1; }

# Point the project's private model libraries (release.toml [models3d.VAR]) at a
# real directory, exported so BOTH kicad-cli and the linter resolve the same
# files. The STOCK KiCad library is deliberately NOT handled here -- it is an
# environment prerequisite (kicad-packages3d / the `-full` image), not something
# to re-implement a package manager for.
setup_models() {
  local out line
  out=$(python3 "$S/model_libs.py" "$proj") || rc=1   # progress/errors go to stderr
  while IFS= read -r line; do
    [ -n "$line" ] && export "$line"
  done <<<"$out"
}

case "$cmd" in
  erc)     gate_erc ;;
  drc)     gate_drc ;;
  3d)      setup_models; gate_3d ;;
  all)     gate_erc; gate_drc; setup_models; gate_3d ;;
  pnp)     python3 "$S/release_ci.py" "$proj" --mode pnp   || rc=1 ;;
  drift)   python3 "$S/release_ci.py" "$proj" --mode drift || rc=1 ;;
  docs)    setup_models; gate_3d
           python3 "$S/release_ci.py" "$proj" --mode docs  || rc=1 ;;   # customer set (not the fab zip)
  check)   gate_erc; gate_drc; setup_models; gate_3d
           python3 "$S/release_ci.py" "$proj" --mode drift || rc=1 ;;
  release) gate_erc; gate_drc; setup_models; gate_3d
           python3 "$S/release_ci.py" "$proj" --mode build || rc=1     # fab zip (PCBA partner)
           python3 "$S/release_ci.py" "$proj" --mode docs  || rc=1 ;;  # STEP/PDF/renders/iBOM (customer)
  *) echo "pcb-release: unknown command '$cmd'" >&2; exit 2 ;;
esac
echo "reports in: $out"
exit $rc
