# scripts/

Implementation called by `../pcb-release.sh` — you normally don't run these
directly (the exception is `release_pcb.py`, the manual workflow, reached via
`pcb-release.sh <project> manual`).

| Script | What it does |
|---|---|
| `release_ci.py`    | Config-driven release: runs the kicad-cli exports, reads the *structured* outputs (`.gbrjob`, pick&place, via-tenting), resolves them against `release.toml`, and emits `release_spec.toml` (committed, drift-checked) + `README-manufacturing.txt`. Modes: `build`, `drift`, `check`, `pnp` (pos≥BOM only). |
| `dnp_lint.py`      | Flags legacy DNP/DNI markers that lack a native KiCad flag, so a part meant to be unpopulated can't silently get placed (migration guard). |
| `model3d_lint.py`  | Resolves every footprint's 3D model path the way KiCad does, before any export. Guards the STEP/render deliverables: kicad-cli reports an unresolvable model on **stdout and exits 0**, and `pcb render` doesn't report it at all, so a hollow STEP is otherwise indistinguishable from a good one. Also flags legacy `:ALIAS:` paths (GUI-only, never resolvable by kicad-cli) and — scoped by BOM membership, not a refdes list — parts that will be placed but have no model. `--print-needed VAR` emits the model list for a sparse fetch; `--no-file-check` does path hygiene only. |
| `generate_pinmap.py` | Firmware pin-map TOML from the schematic + the CI pin/timer/EXTI checks. Connectivity comes entirely from `kicad-cli` netlists (root + the ref's sheet exported standalone, so intent suffixes like `_IRQ` survive); only the symbol's static pin table is read from the `.kicad_sch`. All project specifics — designator, output path, reserved peripherals, grouping, ignores — live in the project's own `pinmap.config.toml` (deliberately not `release.toml`: a pin map is a schematic-stage artifact, settled long before layout). Modes: write, `--check`, `--drift`. |
| `check_report.py`  | Applies the pass/fail policy to a kicad-cli ERC/DRC JSON report — errors gate; warnings only with `--strict`; KiCad-excluded items and `--ignore-type` classes are suppressed. |
| `kicad_netlist.py` | Minimal `kicadxml` netlist parser (value/fields/properties + native flags) for DNP/placement classification. Imported, not run. |
| `release_pcb.py`   | The legacy **interactive** release — prompts you through gerber/BOM/pick&place export, then zips `production/`. Run via `pcb-release.sh <project> manual`. |
