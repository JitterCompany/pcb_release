# Shared console palette. Sourced by pcb-release.sh AND by each project's entry
# script (hardware/tools/pcb.sh), so one run reads as a single consistent stream
# no matter which layer printed a given line -- and so the enable/disable rule
# lives in exactly one place.
#
# Colour when the terminal can render it. GitHub Actions is not a TTY but does
# render ANSI, so opt it in by name. NO_COLOR (https://no-color.org) always wins.
if [ -n "${NO_COLOR:-}" ]; then                       C_OK=; C_ERR=; C_WARN=; C_NOTE=; C_DIM=; C_OFF=
elif [ -t 1 ] || [ "${GITHUB_ACTIONS:-}" = "true" ]; then
  C_OK=$'\033[32m'; C_ERR=$'\033[31m'; C_WARN=$'\033[33m'; C_NOTE=$'\033[36m'
  C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else                                                  C_OK=; C_ERR=; C_WARN=; C_NOTE=; C_DIM=; C_OFF=; fi

# Filter: stdin -> stdout, rendering GitHub workflow commands for humans.
#
#   ::error::[ERC] ERROR: Courtyards overlap @ ...   ->   Error    [ERC] Courtyards overlap @ ...
#
# In Actions the ::...:: form IS the API that produces PR annotations, so there it
# passes through byte-for-byte untouched. Everywhere else it is machine syntax
# leaking into a human's terminal, so translate it: severity as a coloured word,
# the redundant "ERROR:"/"WARNING:" inside the body dropped (the colour says it),
# and any `title=` kept as a prefix. Non-annotation lines pass through unchanged.
annot_render() {
  if [ "${GITHUB_ACTIONS:-}" = "true" ]; then cat; return; fi
  awk -v e="$C_ERR" -v w="$C_WARN" -v n="$C_NOTE" -v off="$C_OFF" '
    {
      line = $0; sev = ""
      if      (line ~ /^::error/)   { sev = "Error";   col = e }
      else if (line ~ /^::warning/) { sev = "Warning"; col = w }
      else if (line ~ /^::notice/)  { sev = "Note";    col = n }
      else { print; next }
      title = ""
      if (match(line, /^::[a-z]+ title=[^:]*::/)) {
        title = substr(line, RSTART, RLENGTH)
        sub(/^::[a-z]+ title=/, "", title); sub(/::$/, "", title)
        title = title ": "
      }
      sub(/^::[a-z]+( title=[^:]*)?::/, "", line)
      # "[ERC] ERROR: x" -> "[ERC] x": the coloured severity already said it
      if (match(line, /^(\[[^]]*\] )?(ERROR|WARNING|NOTICE): /)) {
        body = substr(line, RSTART + RLENGTH)
        lbl = ""
        if (match(line, /^\[[^]]*\] /)) lbl = substr(line, 1, RLENGTH)
        line = lbl body
      }
      printf "  %s%-7s%s %s%s\n", col, sev, off, title, line
    }'
}
