#!/usr/bin/env bash
# pcb_release — KiCad design checks + manufacturing release. THE single entry
# point; everything else lives in scripts/ and is called from here.
#
# Automated (config-driven) workflow — needs a <project>/release.toml:
#   pcb-release.sh PROJECT_DIR check       ERC+schema, DRC+pos>=BOM, release-spec drift
#   pcb-release.sh PROJECT_DIR release     generate gerbers/drill/pos + spec + README
#   pcb-release.sh PROJECT_DIR erc|drc|all just that check gate (used by CI jobs)
#   pcb-release.sh PROJECT_DIR pnp|drift   single checks
#
# Manual (interactive, legacy) workflow — no config, prompts you step by step:
#   pcb-release.sh PROJECT_DIR manual      guided export + zip (scripts/release_pcb.py)
#
# Env: KICAD_IGNORE_TYPES  comma-sep ERC/DRC 'type' keys to suppress as CI-env
#                          noise (headless run has no library table).
#      CHECK_OUT           dir for the JSON reports (default: fresh tmp dir).
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; S="$here/scripts"

proj="${1:?usage: $0 PROJECT_DIR <check|release|erc|drc|all|pnp|drift|manual> [--strict]}"; shift || true
cmd="${1:-check}"; shift || true
strict=""; for a in "$@"; do [ "$a" = "--strict" ] && strict="--strict"; done

# Manual workflow runs IN the project dir (it globs *.kicad_pro there, writes production/).
if [ "$cmd" = "manual" ]; then cd "$proj" && exec python3 "$S/release_pcb.py"; fi

pro=$(ls "$proj"/*.kicad_pro 2>/dev/null | head -1)
[ -n "$pro" ] || { echo "pcb-release: no .kicad_pro in '$proj'" >&2; exit 2; }
stem=$(basename "$pro" .kicad_pro); sch="$proj/$stem.kicad_sch"; pcb="$proj/$stem.kicad_pcb"
ignore="${KICAD_IGNORE_TYPES:-}"; out="${CHECK_OUT:-$(mktemp -d)}"; mkdir -p "$out"
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

case "$cmd" in
  erc)     gate_erc ;;
  drc)     gate_drc ;;
  all)     gate_erc; gate_drc ;;
  pnp)     python3 "$S/release_ci.py" "$proj" --mode pnp   || rc=1 ;;
  drift)   python3 "$S/release_ci.py" "$proj" --mode drift || rc=1 ;;
  check)   gate_erc; gate_drc; python3 "$S/release_ci.py" "$proj" --mode drift || rc=1 ;;
  release) gate_erc; gate_drc; python3 "$S/release_ci.py" "$proj" --mode build || rc=1 ;;
  *) echo "pcb-release: unknown command '$cmd'" >&2; exit 2 ;;
esac
echo "reports in: $out"
exit $rc
