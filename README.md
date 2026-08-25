# pcb_release

Automate KiCad projects — **design checks + manufacturing release**, driven from
one entry script. Point it at a KiCad project directory. Designed to be consumed
as a git submodule.

## Entry point

    pcb-release.sh PROJECT_DIR <command>

### Automated workflow (config-driven — needs `<project>/release.toml`)

    pcb-release.sh <project> check      # ERC+schema, DRC+pos>=BOM, release-spec drift
    pcb-release.sh <project> release    # gerbers/drill/pos + release_spec.toml + README-manufacturing
    pcb-release.sh <project> erc|drc|all   # one check gate (used by CI jobs)

`check` runs the built-in ERC/DRC **and** our schema lints — it passes only if
both pass. `release` regenerates the fab outputs plus a drift-checked spec, so an
accidental layer-count / assembly-side / stackup change is caught on review.

### Manual workflow (interactive, legacy — no config)

    pcb-release.sh <project> manual     # guided step-by-step export + zip

The original interactive script: it prompts you to export gerbers / BOM /
pick&place, sanity-checks them, writes a manufacturing README, and zips
`production/`. Use it for a one-off board or when you don't want the config-driven
pipeline. (Still fully supported.)

## Requirements

`kicad-cli` (KiCad 9/10) on PATH, Python 3.8+. No pip dependencies.

## Config — `release.toml`

Declares fabrication INTENT + requirements + stackup strictness; everything KiCad
already knows (stackup, size, via tenting, SMD/THT placement) is **extracted**,
not restated. Copy `release.toml.example` → `<project>/release.toml`.

## Layout

    pcb-release.sh         the entry point (dispatch + the ERC/DRC gate)
    release.toml.example   template config
    scripts/               implementation — see scripts/README.md
    colors.sh              console palette helper; sourced by pcb-release.sh AND by each
                           project's hardware/tools/pcb.sh, so both layers agree on
                           when to colour (TTY or GitHub Actions; NO_COLOR wins)

## Console output

Every stage ends with exactly one verdict line — `PASS` / `FAIL  <board> / <stage>`,
green/red on a TTY — and nothing else claims a pass or fail.

What a stage prints in between depends on where it runs, because the two audiences want
opposite things:

| stage | terminal | GitHub Actions |
|---|---|---|
| passed | just the verdict line | `::warning` / `::notice` still emitted |
| failed | annotations, then dim detail, then the verdict | same |

`::error` / `::warning` / `::notice` *are* the GitHub checks UI, so under Actions they
always go out — a non-gating warning still belongs on the PR. In a terminal the same
lines are noise on a green run, so there they are buffered and only surface if the stage
actually failed. The verdict itself is deliberately NOT an annotation: the job's own
red/green already says that, a failing stage has already annotated each real finding, and
GitHub only surfaces a limited number of annotations per run — spending that budget on
verdicts would push actual findings out of the UI.

`PCB_VERBOSE=1` shows everything regardless. `NO_COLOR=1` disables colour.

kicad-cli's own `Found N violations` is never shown directly: N counts warnings and
deliberately-ignored types too, so on a PASS it only ever contradicted the verdict. (A
headless run has no library table, so a clean board routinely reports hundreds — 219 on
one of ours, every one of them environment noise.) The gating decision belongs to
`scripts/check_report.py`, and the verdict line is the answer.

## Portability: note for developers

These scripts must run unchanged on **macOS**, which still ships **Bash 3.2** (2007) as
`/bin/bash` and a **BSD** userland. Linux CI will happily accept things a Mac then chokes
on, so the rules are not enforced by simply "it worked for me":

Do not use (Bash 4+ only):

| Avoid | Use instead |
|---|---|
| `${var,,}` / `${var^^}` | `tr '[:upper:]' '[:lower:]'` |
| `declare -A` / `local -A` (associative arrays) | parallel indexed arrays, or a `case` |
| `mapfile` / `readarray` | `while IFS= read -r line; do … done <<<"$x"` |
| `&>>`, `|&` | `>>file 2>&1`, `2>&1 |` |
| `coproc`, `globstar` (`**`) | — |

BSD-userland differences that bite just as hard:

| Avoid | Use instead |
|---|---|
| `mktemp` / `mktemp -d` with no template | `mktemp "${TMPDIR:-/tmp}/name.XXXXXX"` — BSD **requires** a template |
| `sed -i` (bare) | write to a temp file and `mv` |
| `grep -P` | `grep -E` |
| `readlink -f` | `cd "$(dirname "$x")" && pwd` |
| `stat -c`, `date -r` | avoid, or branch on `uname` |

Also note `set -uo pipefail` is used **without** `-e`: every call site checks its own exit
status. Do not add a bare `set -e` inside a function — it stays on for the rest of the run
and turns the first non-zero return into a silent early exit.

Quick audit before committing a shell change:

```bash
grep -nE '\$\{[A-Za-z_][A-Za-z0-9_]*(,,|\^\^)|declare -A|local -A|mapfile|readarray|coproc|&>>|\|&' *.sh
grep -nE 'mktemp[[:space:]]*(-d)?[[:space:]]*\)|readlink -f|grep -P|sed -i |stat -c|date -r ' *.sh
```

If you have Homebrew bash (5.x) on a Mac, `/bin/bash script.sh` still uses 3.2 — test with
that path explicitly.

## License

MIT

