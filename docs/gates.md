# Gates

A gate is one check a board must pass. Every gate is enforced for every board unless
that board's entry in the CI board list says otherwise with `skip=` or `todo=`, see
[Configuring CI](../README.md#running-it-in-ci).

Two of them are KiCad's own. The other three exist because KiCad has no opinion on
them and nothing else would catch the failure before the boards are made.

| gate | fails when |
|---|---|
| `erc` | KiCad ERC reports an error, or the legacy-DNP lint finds a part that would be populated by mistake |
| `drc` | KiCad DRC or schematic parity reports an error, or a BOM part has no pick&place position |
| `3d` | a footprint's 3D model does not resolve, so the STEP would ship with that part missing |
| `drift` | the board no longer matches the `release_spec.toml` committed at its last release |
| `pinmap` | the MCU pin map is invalid, or no longer matches the schematic |

## `erc`

KiCad's own ERC, run headless. A headless run has no symbol or footprint library
table, so it flags every library as "not included" on every project. That is
environment noise rather than a design finding and is suppressed by default, which is
why a new project needs no configuration to get a meaningful first result. Set
`KICAD_IGNORE_TYPES` to change the suppressed set, or to the empty string to suppress
nothing.

Warnings do not gate. To make one gate, raise its severity in the KiCad GUI, where the
decision travels with the project and shows up in review.

### The legacy-DNP lint

KiCad has native `dnp` and `exclude_from_bom` flags, and `--exclude-dnp` on the BOM and
pick&place exports honours those and nothing else. Older designs marked a part as
do-not-populate by convention instead: a value of `DNP`, `DNI*`, `LOGO`, `mousebite` or
`inf`, or a non-empty `dnp` / `dni` field.

A part carrying only the old marker is invisible to `--exclude-dnp`, so it comes back in
the BOM and the pick&place and gets placed, on a board where somebody deliberately meant
to leave it off. The lint flags exactly that case: a legacy marker with no native flag.

The fix is to set the flag that matches the intent. `dnp` for a part that is genuinely
not fitted, `exclude_from_bom` for something that was never a part at all, such as a logo
or a mousebite.

## `drc`

KiCad's own DRC with `--schematic-parity`, so a layout that has drifted from the
schematic fails here rather than at the assembler.

### Pick&place covers the BOM

Separately, every part in the BOM must have a position in the pick&place export. A part
that is bought and paid for but has no placement line is one the assembler cannot fit,
and neither KiCad export complains on its own, because each is internally consistent.

## `3d`

`kicad-cli pcb export step` and `pcb render` treat an unresolvable 3D model as a
*warning*. They emit the STEP anyway, with that part silently absent, and exit 0.
Customers fit-check mechanics against that STEP, so a hollow one is worse than no build
at all. This gate resolves every footprint's model path the way KiCad does, before any
export runs.

It also catches the classic desktop-versus-CI split. A designer's machine still has
`${KICAD9_3DMODEL_DIR}` from an in-place upgrade and private aliases in
`kicad_common.json`, so everything resolves locally while a clean container resolves
none of it. See [custom 3D model libraries](../README.md#custom-3d-model-libraries) for
pointing CI at a repo's own library.

What it reports, errors gating and notes not:

| category | meaning |
|---|---|
| `unresolved` | the path still holds `${VAR}` or `:ALIAS:` and that variable is not defined here |
| `missing` | the path resolved but there is no such file |
| `unreadable` | the file exists but cannot be read, which kicad-cli reports as "Cannot identify actual file type" and reads like a corrupt model |
| `legacy_alias` | the GUI-only `:ALIAS:file` form. Normalise it to `${ALIAS}/file` so kicad-cli can resolve it from the environment |
| `no_model` | a footprint that will be placed has no visible model. Scoped by BOM membership, so test points, net ties, fiducials and logos are excused while a real part is not |
| `hidden` | a model is present but hidden, so it is in neither the STEP nor the renders |
| `dnp` | informational: a model on a do-not-populate part |

## `drift`

A release writes `release_spec.toml`, a resolved description of the board as built:
layer count, finished thickness, board size, via treatment, assembly sides, surface
finish, stackup. You commit it. This gate regenerates it and diffs.

So it fails when the board changed since its last release, and the diff names what
changed. An accidental layer-count, assembly-side or stackup change is caught in review
rather than at the fab. Until a board has ever been released it has no spec, and the
gate says so rather than reporting drift.

Note the difference between the two files. You write `release.toml`, which declares
intent. The tool generates `release_spec.toml`, which records the result.

This gate also runs the board **identity checks**: the board's name on the silkscreen,
its name in both title blocks, and a logo footprint. None of those show up in ERC, DRC or
the gerber job, and each can be turned off individually. See
[identity checks](release-toml.md#identity-checks).

## `pinmap`

Only for boards with an MCU, and only when the project has a `pinmap.config.toml`.
A board with no MCU should carry `skip=pinmap` in the board list; without the config
file the gate fails rather than passing having checked nothing.

The map itself is generated from KiCad's own netlist, never from pin geometry. It is
committed, so firmware has a reference and any change to it shows up in review.

The gate is two checks that are deliberately kept apart, so neither can mask the other:

* **validation.** Fixed-function pins (SWDIO/SWCLK, LSE on the OSC32 pins), a net driven
  from more than one MCU pin, the same peripheral function assigned twice, a peripheral
  the firmware reserves, an analog-tagged pin with no ADC channel, and EXTI collisions.
  EXTI line N is shared by pin N of every port, so only one port's pin N can be an
  interrupt source. Suffix a net `_IRQ` to declare the intent and have it checked.
* **drift.** Regenerate and diff against the committed map. Any change fails, because a
  changed pin map is a breaking change for firmware.
