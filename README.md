# pcb_release

Design checks and manufacturing releases for KiCad projects, from one entry script.
Point it at a KiCad project directory. Useful both on the command line, to cut a board
release, and as a CI workflow.

## What it checks

Five gates. Two are KiCad's own, the other three exist because nothing else would catch
the failure before the boards are made.

| gate | fails when |
|---|---|
| `erc` | KiCad ERC reports an error, or a part carries an old do-not-populate marker that modern exports ignore, so it would be populated by mistake |
| `drc` | KiCad DRC or schematic parity reports an error, or a BOM part has no pick&place position |
| `3d` | a footprint's 3D model does not resolve, so the STEP would ship with that part missing |
| `drift` | the board no longer matches the spec committed at its last release |
| `pinmap` | the MCU pin map is invalid, or no longer matches the schematic |

Every gate is enforced for every board unless that board says otherwise, so a new board
cannot go silently unchecked. Details in **[Gates](docs/gates.md)**.

## What it produces

A release writes `production/` for the PCBA partner (gerbers, drill, pick&place, BOM and
a fab-spec checklist) and `customer/` for whoever receives the board (STEP, schematic and
layout PDFs, renders, interactive BOM), and zips both. Details in
**[Deliverables](docs/deliverables.md)**.

## Install

Add the submodule, add two workflow files, add a `release.toml` per board. See
**[Install](docs/install.md)**.

## Running it locally

    pcb-release.sh PROJECT_DIR <command>

| command | does |
|---|---|
| `check` | every gate: ERC and schema, DRC and pick&place, 3D, pin map, release-spec drift |
| `release` | the full manufacturing release, see [Deliverables](docs/deliverables.md) |
| `erc` \| `drc` \| `3d` \| `drift` \| `pinmap` | one gate on its own |
| `bom` | `BOM_<board>.csv` beside the project, to read the parts over |
| `manual` | the legacy interactive workflow, see below |

`check` is what CI runs, so a green local run predicts a green CI run, provided both use
the same version of this tool ([which version runs](docs/install.md#which-version-runs)).

`PCB_VERBOSE=1` shows everything a passing stage would otherwise keep quiet.
`NO_COLOR=1` disables colour.

### Exporting the BOM on its own

    pcb-release.sh hardware/my-board bom       # -> hardware/my-board/BOM_my-board.csv

The same BOM the release ships, for reading the parts over beforehand: grouped by value
and footprint, DNP excluded, footprints shortened per `bom.readable_footprints`. Not a
gate, so `skip=` and `todo=` do not apply, and it needs no `release.toml`.

### Manual workflow (legacy, interactive, no config)

    pcb-release.sh <project> manual     # guided step-by-step export + zip

The original interactive script. It prompts you to export gerbers, BOM and pick&place,
sanity-checks them, writes a manufacturing README and zips `production/`. Useful for an
existing design or a quick one-off board, but the automated workflow above is
recommended.

## Running it in CI

The board list is the only thing a project has to state. Everything else has a default.

```yaml
with:
  tools: tools/pcb_release         # optional, see docs/install.md
  project-dirs: |                  # your boards, relative to the repo root
    my-product/my-board
    my-product/my-module    skip=pinmap
    my-legacy-board         todo=drc,3d,drift
```

A single board can also be given as `project-dir: hardware/my-board`.

### Gate exceptions

Boards pass all gates unless the board list says otherwise:

| field | meaning | output |
|---|---|---|
| *(nothing)* | enforced, a failure fails the build | `PASS` / `FAIL` |
| `skip=` | gate not applicable, e.g. no MCU means no pin map | silent |
| `todo=` | gate applies but is known to fail for now | warning every run, does not fail |

Gate names are `erc`, `drc`, `3d`, `drift`, `pinmap`, in any order and any combination. A
typo is a hard error rather than a quietly unenforced gate.

Naming one board in a local run enforces that board's `todo=` gates, which is how you
find out whether one has gone green. Its `skip=` gates stay skipped, because a gate that
cannot apply to the board has nothing to tell you either way.

### custom 3D model libraries

*Not needed in most cases*

Footprints that reference models through your own KiCad path variable need it named in
the workflow, one `NAME=PATH` per line, path relative to the repo root:

```yaml
with:
  model-dirs: |
    MY_MODEL_DIR=libs/my_3d_models
```

Set it on **both** workflows. The name must work as an environment variable (letters,
digits, underscores), which is how `kicad-cli` resolves it. `${JITTER}` needs no entry.

### Project-specific checks

*Not needed in most cases*

Set `extra-checks: path/to/my-checks.sh` to run your own script once per board, as
`<script> <project-dir>`, after the standard gates.

## Releasing a board

1. Push a `hw-v*` tag, or start the release workflow from the Actions tab. A first
   release needs no `release_spec.toml`, it writes one.
2. Download the artifacts. Per board there are three: `production__<board>__<date>` for
   the PCBA partner, `customer__<board>__<date>` with the STEP, PDFs, renders and
   interactive BOM, and `release-spec__<board>`.
3. Commit that `release_spec.toml`. It records the board as built, and the `drift` gate
   fails later if the board stops matching it. Until a board has one, `drift` fails with
   "does not exist yet", so a brand new board only goes fully green after its first
   release.

Locally it is one command. `pcb-release.sh hardware/my-board release` writes the same
outputs and puts the spec straight into the project.

## Config: `release.toml`

One per board, beside its `.kicad_pro`. It declares fabrication **intent**: surface
finish, soldermask and silkscreen colour, via treatment, stackup strictness. Everything
KiCad already knows, such as stackup, board size, via tenting and SMD/THT placement, is
extracted from the design rather than restated here.

```toml
[board]
# name = "my-board-2"      # deliverable name, defaults to the KiCad project name

[fab]
surface_finish   = "ENIG"
soldermask_color = "green"
via_treatment    = "tented both sides"

[stackup]
spec = "standard"          # standard | impedance | controlled
```

Anything omitted takes its default, and omitting a whole section is fine. Every option is
listed in the **[`release.toml` reference](docs/release-toml.md)**.

Do not confuse it with `release_spec.toml`, which you do not write: a release generates
that one to record the board as built, and the `drift` gate watches it.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the repo layout, the console-output rules and
the Bash 3.2 / BSD portability constraints.

## License

MIT
