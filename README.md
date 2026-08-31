# pcb_release

Automate KiCad projects: **design checks + manufacturing release**, driven from
one entry script. Point it at a KiCad project directory. Designed to be consumed
as a git submodule.

## Entry point

    pcb-release.sh PROJECT_DIR <command>

### Automated workflow (config-driven, needs `<project>/release.toml`)

    pcb-release.sh <project> check      # ERC+schema, DRC+pos>=BOM, release-spec drift
    pcb-release.sh <project> release    # gerbers/drill/pos + release_spec.toml + README-manufacturing
    pcb-release.sh <project> erc|drc|all   # one check gate (used by CI jobs)
    pcb-release.sh <project> bom        # BOM_<board>.csv beside the project, to read through

`check` runs the built-in ERC/DRC **and** our schema lints. It passes only if
both pass. `release` regenerates the fab outputs plus a drift-checked spec, so an
accidental layer-count / assembly-side / stackup change is caught on review.

### Telling CI which boards exist

The board list is the only thing a project states. Everything else lives here, so a
`git pull` in the submodule is enough to pick up improvements.

```yaml
# one board
with: { project-dir: hardware/my-board }

# several: one per line, policy optional and in any order
with:
  project-dirs: |
    hardware/my-board
    hardware/my-board/my-module   skip=pinmap
    hardware/my-old-board         skip=pinmap todo=drc,3d,drift
```

**Every gate is enforced for every board** unless that board says otherwise. A new board
therefore cannot go silently unchecked, and a gate cannot be forgotten, only exempted on
purpose.

| field | meaning | output |
|---|---|---|
| *(nothing)* | enforced, a failure fails the build | `PASS` / `FAIL` |
| `skip=` | structurally impossible here, e.g. no MCU means no pin map | silent |
| `todo=` | applies, just not green yet | warning every run, does not fail |

Gate names are `erc`, `drc`, `3d`, `drift`, `pinmap`. A typo is a hard error rather than a
quietly unenforced gate. `pinmap` covers both pin-map gates, because they are one concern
to a board owner and naming them separately only invites listing one of the two.

Naming one board in a local run enforces that board's `todo=` gates, which is how you find
out whether a gate has gone green. Its `skip=` gates stay skipped, because a gate that
cannot apply to the board has nothing to tell you either way.

Both workflows produce **one job per board**, so the checks UI reads `hardware (my-board)`.

### Project-specific checks

Set `extra-checks: hardware/tools/my-checks.sh` to run your own script once per board, as
`<script> <project-dir>`, after the standard gates. Optional and absent by default. This
is the supported way to keep a project-local script without CI depending on it.

### A repo with its own 3D model library

Footprints that reference models through your own KiCad path variable need it named in
the workflow, one `NAME=PATH` per line, path relative to the repo root:

```yaml
with:
  model-dirs: |
    MY_MODEL_DIR=libs/my_3d_models
```

Set it on **both** workflows. The name must work as an environment variable (letters,
digits, underscores), which is how `kicad-cli` resolves it. `${JITTER}` needs no entry.

### One job, every board

The checks run as a **single job** that loops over the boards. The gates take seconds per
board while pulling the KiCad image does not, so a job per board would multiply the only
expensive part of the run by the number of boards to save a few seconds of wall clock.
Each board still prints its own `PASS`/`FAIL` lines, and one failing board never stops the
others.

The release workflow does use one job per board, because each board uploads its own
artifact pair and the per-board work there is substantial.

### Manual workflow (interactive, legacy, no config)

    pcb-release.sh <project> manual     # guided step-by-step export + zip

The original interactive script: it prompts you to export gerbers / BOM /
pick&place, sanity-checks them, writes a manufacturing README, and zips
`production/`. Use it for a one-off board or when you don't want the config-driven
pipeline. (Still fully supported.)

## Requirements

`kicad-cli` (KiCad 9/10) on PATH, Python 3.8+. No pip dependencies.

## Config: `release.toml`

Declares fabrication INTENT + requirements + stackup strictness. Everything KiCad
already knows (stackup, size, via tenting, SMD/THT placement) is **extracted**,
not restated. Copy `release.toml.example` → `<project>/release.toml`.

`release.toml.example` is deliberately short, just the handful you normally set. Every
option is listed here. Anything omitted takes the default, and a config that omits a
whole section is fine.

### `[board]`

| key | default | meaning |
|---|---|---|
| `name` | KiCad project name | Deliverable name (zips, BOM, STEP, PDFs) and the text required on the silkscreen. Set it when the board is called something other than its project file, e.g. project `my-project`, board `my-board-2`. |
| `skip` | `[]` | Identity checks to skip: `"name"`, `"title"`, `"logo"`. Rarely needed. |
| `date` | *unset* | `"auto"` lets a `release` build stamp today into both title blocks. Unset, or `"skip"`, leaves them alone and trusts the value filled in by hand. |

Three checks that nothing else catches, because none of them show up in ERC, DRC or the
gerber job. Each can be turned off individually with `skip`.

* **name on silkscreen.** The board must carry its own name on F.SilkS or B.SilkS.
  Matching is lenient on purpose. Case is ignored, and `-`, `_` or whitespace may be any
  separator, or a split across several text items in any order. A board silkscreening
  `MyModule-12` above `sensor-board` satisfies `MyModule-12-sensor-board`, including when the two
  lines are one multi-line text item. Reference designators and values are excluded,
  because they are not the board's name and counting them would make the check pass on
  almost anything. Set `name` to whatever is actually silkscreened, or
  `skip = ["name"]` to turn the check off.
* **title block.** The root schematic and the layout must both carry the board name in
  their title block, because that is what a reader sees on every exported PDF. Matching
  is the same lenient comparison, and the name only has to appear in the title, so
  `"my-board-2 (main)"` is fine. A trailing revision may live in the title block's own
  Revision field instead of being repeated in the title, so title `my-board` with
  Revision `2` satisfies the name `my-board-2`. Use `skip = ["title"]` to turn it off.
* **logo present.** At least one footprint whose library or reference says `logo`. Use
  `skip = ["logo"]` to turn it off.

The **title block date** is separate and is only touched when you cut a release. `drift`
and `check` stay strictly read-only, so a CI push never rewrites a design file.

There are two states and nothing in between. `date = "auto"` stamps today into the root
schematic and the layout, on the grounds that you are publishing the board today, so that
is its date. Leave `date` unset, or set it to `"skip"`, and the tool never looks at the
field. The value you typed by hand then stands, which is the right answer for a board
whose design genuinely has not moved in years.

`"auto"` is the only setting that writes anything. It edits that one field and nothing
else, prints a notice so you remember to commit the change, and reports a file that has
no title block rather than inventing one.

### `[fab]`

| key | default | meaning |
|---|---|---|
| `surface_finish` | `"any"` | e.g. `"ENIG"`, `"HASL-LF"` |
| `soldermask_color` | `"any"` | taken from the KiCad stackup if set there |
| `silkscreen_color` | `"any"` | |
| `via_treatment` | `"any"` | e.g. `"tented both sides"`, cross-checked against KiCad's tenting/plugging/filling |
| `special_layers` | `[]` | e.g. `["Coating_top","Coating_bottom"]` for conformal coating |
| `flex` | `"none"` | `"none"` or a stiffener spec |
| `notes` | `"any"` | free text for the fab |

### `[stackup]`

| key | default | meaning |
|---|---|---|
| `spec` | `"standard"` | `standard` = any standard N-layer at the finished thickness, no impedance control · `impedance` = impedance targets binding, stackup reference-only · `controlled` = build the exact dielectric stackup |
| `impedance_note` | `""` | e.g. `"50R SE on L1 (ref plane L2)"`. Only valid when `spec` is not `standard`. |

### `[assembly]` / `[stencil]`

| key | default | meaning |
|---|---|---|
| `assembly.sides` | `"auto"` | `auto` assembles every side that has parts · `none` = bare PCB · `top`/`btm`/`both` to force |
| `stencil.force` | `""` | `""` auto-derives from SMD placement. Use `top`/`btm`/`both`/`none` to force |

### `[requirements]`

| key | default | meaning |
|---|---|---|
| `rohs` | `true` | RoHS-compliant (lead-free) build |
| `ul94_v0` | `true` | laminate flammability rating UL94 V-0 |
| `ipc_class` | *unset* | IPC-A-600 / IPC-A-610 class (`2` or `3`) |

### `[customer]` / `[bom]`

| key | default | meaning |
|---|---|---|
| `customer.step_exclude_dnp` | `true` | STEP shows the board as assembled (DNP parts left out). Affects the STEP only, renders always include DNP. |
| `customer.render_preset` | `"follow_pcb_editor"` | physical layers only. `"follow_plot_settings"` also paints fab-intent layers (impedance, coating) over the board. |
| `bom.readable_footprints` | `true` | shorten footprints (`C 0603`) instead of raw KiCad names |

### Exporting the BOM on its own

    pcb-release.sh hardware/my-board bom       # -> hardware/my-board/BOM_my-board.csv

The same BOM the release ships, for reading the parts over beforehand: grouped by value
and footprint, DNP excluded, footprints shortened per `bom.readable_footprints`. Not a
gate, so `skip=` / `todo=` do not apply, and it needs no `release.toml`.

## Layout

    pcb-release.sh         one board: dispatch + the ERC/DRC gate
    pcb-checks.sh          a project's whole board list, what CI and the local
                           wrapper both call. Not inline in the workflow, so it can
                           be run and tested outside CI.
    scripts/boards.py      parses and validates the board list. Emits it as lines
                           for a shell loop, or as JSON for the release matrix.
    release.toml.example   template config
    scripts/               implementation, see scripts/README.md
    colors.sh              console palette helper, sourced by pcb-release.sh AND by each
                           project's hardware/tools/pcb.sh, so both layers agree on
                           when to colour (TTY or GitHub Actions, NO_COLOR wins)

## Console output

Every stage ends with exactly one verdict line, `PASS` / `FAIL  <board> / <stage>`,
green/red on a TTY, and nothing else claims a pass or fail.

What a stage prints in between depends on where it runs, because the two audiences want
opposite things:

| stage | terminal | GitHub Actions |
|---|---|---|
| passed | just the verdict line | `::warning` / `::notice` still emitted |
| failed | annotations, then dim detail, then the verdict | same |

`::error` / `::warning` / `::notice` *are* the GitHub checks UI, so under Actions they
always go out, because a non-gating warning still belongs on the PR. In a terminal the same
lines are noise on a green run, so there they are buffered and only surface if the stage
actually failed. The verdict itself is deliberately NOT an annotation: the job's own
red/green already says that, a failing stage has already annotated each real finding, and
GitHub only surfaces a limited number of annotations per run, so spending that budget on
verdicts would push actual findings out of the UI.

`PCB_VERBOSE=1` shows everything regardless. `NO_COLOR=1` disables colour.

kicad-cli's own `Found N violations` is never shown directly: N counts warnings and
deliberately-ignored types too, so on a PASS it only ever contradicted the verdict. (A
headless run has no library table, so a clean board routinely reports hundreds, 219 on
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
| `coproc`, `globstar` (`**`) | n/a |

BSD-userland differences that bite just as hard:

| Avoid | Use instead |
|---|---|
| `mktemp` / `mktemp -d` with no template | `mktemp "${TMPDIR:-/tmp}/name.XXXXXX"`, BSD **requires** a template |
| `sed -i` (bare) | write to a temp file and `mv` |
| `grep -P` | `grep -E` |
| `readlink -f` | `cd "$(dirname "$x")" && pwd` |
| `stat -c`, `date -r` | avoid, or branch on `uname` |

Also note `set -uo pipefail` is used **without** `-e`: every call site checks its own exit
status. Do not add a bare `set -e` inside a function. It stays on for the rest of the run
and turns the first non-zero return into a silent early exit.

Quick audit before committing a shell change:

```bash
grep -nE '\$\{[A-Za-z_][A-Za-z0-9_]*(,,|\^\^)|declare -A|local -A|mapfile|readarray|coproc|&>>|\|&' *.sh
grep -nE 'mktemp[[:space:]]*(-d)?[[:space:]]*\)|readlink -f|grep -P|sed -i |stat -c|date -r ' *.sh
```

If you have Homebrew bash (5.x) on a Mac, `/bin/bash script.sh` still uses 3.2, so test with
that path explicitly.

## License

MIT

