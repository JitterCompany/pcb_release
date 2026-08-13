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

## License

MIT
