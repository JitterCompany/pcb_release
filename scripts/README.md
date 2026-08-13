# scripts/

Implementation called by `../pcb-release.sh` — you normally don't run these
directly (the exception is `release_pcb.py`, the manual workflow, reached via
`pcb-release.sh <project> manual`).

| Script | What it does |
|---|---|
| `release_ci.py`    | Config-driven release: runs the kicad-cli exports, reads the *structured* outputs (`.gbrjob`, pick&place, via-tenting), resolves them against `release.toml`, and emits `release_spec.toml` (committed, drift-checked) + `README-manufacturing.md`. Modes: `build`, `drift`, `check`, `pnp` (pos≥BOM only). |
| `dnp_lint.py`      | Flags legacy DNP/DNI markers that lack a native KiCad flag, so a part meant to be unpopulated can't silently get placed (migration guard). |
| `check_report.py`  | Applies the pass/fail policy to a kicad-cli ERC/DRC JSON report — errors gate; warnings only with `--strict`; KiCad-excluded items and `--ignore-type` classes are suppressed. |
| `kicad_netlist.py` | Minimal `kicadxml` netlist parser (value/fields/properties + native flags) for DNP/placement classification. Imported, not run. |
| `release_pcb.py`   | The legacy **interactive** release — prompts you through gerber/BOM/pick&place export, then zips `production/`. Run via `pcb-release.sh <project> manual`. |
