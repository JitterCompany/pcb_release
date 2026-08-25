# Shared console palette. Sourced by pcb-release.sh AND by each project's entry
# script (hardware/tools/pcb.sh), so one run reads as a single consistent stream
# no matter which layer printed a given line -- and so the enable/disable rule
# lives in exactly one place.
#
# Colour when the terminal can render it. GitHub Actions is not a TTY but does
# render ANSI, so opt it in by name. NO_COLOR (https://no-color.org) always wins.
if [ -n "${NO_COLOR:-}" ]; then                       C_OK=; C_ERR=; C_DIM=; C_OFF=
elif [ -t 1 ] || [ "${GITHUB_ACTIONS:-}" = "true" ]; then
  C_OK=$'\033[32m'; C_ERR=$'\033[31m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else                                                  C_OK=; C_ERR=; C_DIM=; C_OFF=; fi
