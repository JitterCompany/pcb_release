# `release.toml` reference

One per board, beside its `.kicad_pro`. It declares fabrication **intent**. Everything
KiCad already knows, such as stackup, board size, via tenting and SMD/THT placement, is
extracted from the design rather than restated here.

Anything omitted takes its default, and omitting a whole section is fine.
[`release.toml.example`](../release.toml.example) holds just the handful you normally
set, and the first run of a board without a config drops a copy beside the project.

## `[board]`

| key | default | meaning |
|---|---|---|
| `name` | KiCad project name | Deliverable name (zips, BOM, STEP, PDFs) and the text required on the silkscreen. Set it when the board is called something other than its project file, e.g. project `my-project`, board `my-board-2`. |
| `skip` | `[]` | Identity checks to skip: `"name"`, `"title"`, `"logo"`. Rarely needed. |
| `date` | *unset* | `"auto"` stamps today into both title blocks on a `release`. Unset, or `"skip"`, leaves them alone. |

### Identity checks

Checked by the [`drift` gate](gates.md#drift) and again when you cut a release. Nothing
else catches these, since none of them show up in ERC, DRC or the gerber job.

| check | requires | turn off with |
|---|---|---|
| name | the board name on `F.SilkS` or `B.SilkS` | `skip = ["name"]` |
| title | the board name in the title block of both the root schematic and the layout | `skip = ["title"]` |
| logo | at least one footprint whose library or reference says `logo` | `skip = ["logo"]` |

Matching is lenient on purpose. Case is ignored, `-`, `_` and whitespace are
interchangeable separators, and the name may be split across several silkscreen items in
any order, including one multi-line item, so `MyModule-12` above `sensor-board` satisfies
`MyModule-12-sensor-board`. Reference designators and values do not count, or the check
would pass on almost anything. In a title block the name only has to appear, so
`"my-board-2 (main)"` is fine, and a trailing revision may live in the Revision field
instead of the title, so title `my-board` with Revision `2` satisfies `my-board-2`.

If a board legitimately carries a different name, set `name` to what is actually on it
rather than skipping the check.

### `date`

`"auto"` is the only setting that writes to a design file, and only on `release`. `check`
and `drift` stay read-only, so a CI push never rewrites your design. It stamps today into
the root schematic and the layout, touches that one field and nothing else, prints a
notice so you remember to commit it, and reports a file that has no title block rather
than inventing one.

Unset, or `"skip"`, means the tool never looks at the field. That is the right answer for
a board whose design genuinely has not moved in years.

## `[fab]`

| key | default | meaning |
|---|---|---|
| `surface_finish` | `"any"` | e.g. `"ENIG"`, `"HASL-LF"` |
| `soldermask_color` | `"any"` | taken from the KiCad stackup if set there |
| `silkscreen_color` | `"any"` | |
| `via_treatment` | `"any"` | e.g. `"tented both sides"`, cross-checked against KiCad's tenting/plugging/filling |
| `special_layers` | `[]` | e.g. `["Coating_top","Coating_bottom"]` for conformal coating |
| `flex` | `"none"` | `"none"` or a stiffener spec |
| `notes` | `"any"` | free text for the fab |

## `[stackup]`

| key | default | meaning |
|---|---|---|
| `spec` | `"standard"` | `standard` = any standard N-layer at the finished thickness, no impedance control · `impedance` = impedance targets binding, stackup reference-only · `controlled` = build the exact dielectric stackup |
| `impedance_note` | `""` | e.g. `"50R SE on L1 (ref plane L2)"`. Only valid when `spec` is not `standard`. |

## `[assembly]` / `[stencil]`

| key | default | meaning |
|---|---|---|
| `assembly.sides` | `"auto"` | `auto` assembles every side that has parts · `none` = bare PCB · `top`/`btm`/`both` to force |
| `stencil.force` | `""` | `""` auto-derives from SMD placement. Use `top`/`btm`/`both`/`none` to force |

## `[requirements]`

| key | default | meaning |
|---|---|---|
| `rohs` | `true` | RoHS-compliant (lead-free) build |
| `ul94_v0` | `true` | laminate flammability rating UL94 V-0 |
| `ipc_class` | *unset* | IPC-A-600 / IPC-A-610 class (`2` or `3`) |

## `[customer]` / `[bom]`

| key | default | meaning |
|---|---|---|
| `customer.step_exclude_dnp` | `true` | STEP shows the board as assembled (DNP parts left out). Affects the STEP only, renders always include DNP. |
| `customer.render_preset` | `"follow_pcb_editor"` | physical layers only. `"follow_plot_settings"` also paints fab-intent layers (impedance, coating) over the board. |
| `bom.readable_footprints` | `true` | shorten footprints (`C 0603`) instead of raw KiCad names |
