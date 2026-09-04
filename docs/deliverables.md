# What a release produces

`pcb-release.sh <project> release` writes two directories beside the project and zips
each one. In CI they arrive as artifacts, see
[Releasing a board](../README.md#releasing-a-board).

Everything is named after the board, which is the KiCad project name unless
`[board] name` in `release.toml` overrides it. The name is not just the filename: the
gerbers are exported from a copy carrying that name, so the `%TF.ProjectId` inside them
matches too, rather than still naming the KiCad project.

## `production/` — for the PCBA partner

| file | what it is |
|---|---|
| `<board>-F_Cu.gtl`, `-B_Cu.gbl`, `-In1_Cu.g1`, … | copper, one gerber per layer |
| `<board>-F_Mask.gts`, `-B_Mask.gbs` | solder mask |
| `<board>-F_Paste.gtp`, `-B_Paste.gbp` | solder paste, the stencil layers |
| `<board>-F_Silkscreen.gto`, `-B_Silkscreen.gbo` | silkscreen |
| `<board>-F_Fab.gbr`, `-B_Fab.gbr` | fabrication drawings |
| `<board>-Edge_Cuts.gm1` | board outline |
| `<board>-PTH.drl`, `<board>-NPTH.drl` | plated and non-plated drill files |
| `<board>-job.gbrjob` | Gerber X2 job file, the machine-readable stackup and finish |
| `all-pos.csv`, `smd-pos.csv` | pick&place, everything and SMD-only |
| `BOM_<board>.csv` | the assembly BOM, grouped by value and footprint, DNP excluded |
| `README-manufacturing.txt` | the fab-spec checklist, see below |

Special layers named in `release.toml` under `[fab] special_layers`, conformal coating
for example, are exported alongside these.

### `README-manufacturing.txt`

The one file a human at the fab reads. It states what you are ordering rather than
leaving it to be inferred from the gerbers: layer count, finished thickness, board size,
surface finish, soldermask and silkscreen colour, via treatment, stackup strictness,
which sides are assembled, RoHS and UL94 requirements, IPC class, and any free-text
notes. Each value is resolved from the design first and `release.toml` second, so it
cannot contradict the gerbers it ships with.

## `customer/` — for whoever receives the board

| file | what it is |
|---|---|
| `<board>.step` | 3D model of the assembled board, for mechanical fit checks |
| `<board>-schematic.pdf` | the schematic |
| `<board>-layout.pdf` | the layout, multipage with borders and title blocks |
| `<board>-render-top.png`, `-render-bottom.png` | photo-realistic renders |
| `*.html` | interactive HTML BOM, an assembly aid. The name comes from InteractiveHtmlBom, and a project's own `ibom.config.ini` can change it |

The STEP leaves DNP parts out by default, so it shows the board as actually assembled.
Renders always include them, because kicad-cli has no DNP filter for rendering. Both are
configurable under `[customer]`, see the
[`release.toml` reference](release-toml.md).

The interactive BOM is best-effort. It shells out to InteractiveHtmlBom, which builds a
`wx.App()` even in CLI mode and so needs a display. Where none is available it is skipped
with a warning rather than failing the release, because it is an assembly aid and not a
dimensional deliverable.

## Zips and naming

Both directories are also zipped as `production__<board>__<date>.zip` and
`customer__<board>__<date>.zip`. In CI a build number is appended, taken from
`$BUILD_NUMBER` or GitHub's `$GITHUB_RUN_NUMBER`, so two builds of the same board on the
same day stay distinguishable. A local build simply omits it.

CI uploads the two directories rather than the zips, because `upload-artifact` always
zips what it is given and would otherwise produce a zip containing a zip. Downloading an
artifact therefore yields one archive named the same as the local build's.

## `release_spec.toml`

Written into the project directory, not into either deliverable set, and meant to be
committed. It records the board as built so the [`drift` gate](gates.md#drift) can tell
you later when the board no longer matches. CI uploads it as its own artifact,
`release-spec__<board>`.
