# The bomcheck database

Plain TOML, one file per datasheet, meant to be read and edited by hand. The
tool changes these files line by line and never regenerates them, so your
comments stay.

    hardware/attestations/
      exemptions.toml        parts excused from the datasheet rule
      footprints.toml        footprint polarity marks a person confirmed
      waivers.toml           design findings accepted, or known issues of a rev
      reference/datasheet/   THE DATABASE. Only these files are checked against.
      reference/interface/   signed interface tables (see below)
      reference/draft/       work in progress: machine-read DRAFT-*, blank STUB-*,
                             interface tables in draft/interface/
      review/                working files; regenerated, safe to delete, never
                             committed (bomcheck puts a .gitignore in it)

## Two places for signed tables

A signed table holds what a datasheet says about one MPN, so it does not depend
on the board. It can live in two places:

| place | for | example |
|---|---|---|
| the component library: `<library>/attestations/reference/datasheet/` | parts from that library or from KiCad's own libraries | `libraries/KicadComponents/attestations/...` |
| the board's repo: `attestations/reference/datasheet/` | parts with a project-local symbol, or a board-specific override | `attestations/...` beside the board folder |

- The component library is found from the project's `sym-lib-table`: a symbol
  library in another git repository than the board (a submodule). With more than
  one, `--library DIR` or `BOMCHECK_LIBRARY` says which one gets new tables.
- A board reads both. For the same MPN, the board's own table wins.
- When the wizard signs a table it asks where to save it, and proposes the
  library unless the symbol is project-local. `--save-to library|project`
  answers for every part, for scripted runs.
- A table saved in the library reaches other boards only after a commit there
  (a PR in the library repo) and a submodule update in those boards. `bomcheck`
  lists the library's tables that are not committed yet.
- Interface tables follow the same rules, in `reference/interface/`. For the
  same symbol, the board's own wins.
- Everything else (drafts, waivers, exemptions, review files) stays with the board.

## A reference file

```toml
doc_id       = "SCDS477A"          # the manufacturer's document number
manufacturer = "Texas Instruments"
origin       = "datasheet"         # "datasheet" = machine-read, "manual" = typed
extracted_by = "bomcheck-extract"  # or the person who typed it
reviewed_by  = "A. Person"         # who checked it against the PDF
reviewed_on  = "2026-09-30"

  [[variant]]                      # one per package in the document
  part         = "TMUX1309APWR"
  package      = "TSSOP-16_4.4x5mm_P0.65mm"
  mpns         = ["TMUX1309APWR"]  # exact order codes this variant covers
  pin_count    = 16                # from the package, never from the symbol
  active_low   = ["6"]             # optional: marked active-low in prose only

    [variant.pins]                 # pin number = name AS THE DATASHEET PRINTS IT
    1 = "S0B"
    6 = "EN"
    7 = "NC"

    [[variant.alias_for]]          # optional: aliases for these symbols only
    symbols      = ["Jitter_IC:TMUX1309A"]

    [variant.alias_for.pins]       # pin = the symbol's name, accepted
    6 = "~{EN}"   # datasheet: EN
```

Rules the checker applies:

- **Only signed files count.** A file in `reference/datasheet/` without
  `reviewed_by` is an error. Sign by adding the two lines, or through the wizard.
- **Names are the datasheet's**, never copied from the symbol. A copy of the
  symbol would agree with the thing it is there to check.
- **An alias is per pin and per exact name.** `1 = "S"` accepts the
  symbol calling pin 1 `S`. If the symbol later puts `G` on pin 1, or `S` on a
  different pin, that is reported again.
- **An alias is per symbol.** It names a symbol's pin, so it is only known
  to be right for the symbol it was accepted on. See below.

### Aliases and symbols

The table is keyed to the part, but an alias is about a symbol too. So an
alias block lists the symbols it was accepted for:

```toml
    [[variant.alias_for]]
    symbols      = ["Jitter_Connectors:SL23_Header_2x25"]   # or a bare symbol name

    [variant.alias_for.pins]
    1 = "IO_1"   # datasheet: 1
    2 = "GND"    # datasheet: 2

    [[variant.alias_for]]          # another symbol, other names
    symbols      = ["Jitter_Connectors:Other_Header_2x25"]

    [variant.alias_for.pins]
    1 = "SDA"    # datasheet: 1
```

- `symbols` uses the same matching as an interface table: `Lib:Name`, or
  `Name` for that symbol in any library.
- One block per set of names. Symbols with the same names share a block.
- A block applies only to the symbols it lists. On another symbol of the
  same part, the pins its aliases would settle are reported as
  `alias_other_symbol`, naming both symbols, not as plain differences. The
  wizard then asks whether the other symbol has the same pinout: if yes, it
  is added to the block; if not, you confirm or type each name for it, and
  it gets its own block.
- Once a symbol has a block of its own, even an empty one, the blocks of
  other symbols are not looked at for it. Its remaining differences are
  plain `pin_difference` items.
- An older file has `[variant.symbol_alias]` (pin = name, no `symbols`). It
  applies to every symbol, as before. The wizard writes new aliases into
  blocks with the symbol.
- **`pin_count` above the number of listed pins** marks the table as partial.
  The listed pins are still compared, and the line stays open until the rest
  are typed in.

## Numbers-only parts

A connector or a choke datasheet numbers its pins and names none of them. Its
table claims only the numbering:

```toml
  [[variant]]
  part         = "691322310003"
  mpns         = ["691322310003"]
  pin_count    = 3
  pin_numbers  = ["1", "2", "3"]
```

The check is then that the symbol has exactly these pin numbers. The wizard
also asks which pins are joined inside the part (`joined`), the winding dots
(`dots`) and whether the footprint is numbered like the datasheet's drawing
(`pad_layout = "same"` or `"differs"`).

A numbers-only table can also belong to a symbol that names its pins: a
plain socket on a symbol that names the pins after an interface. The table
then checks the numbering only. The names are checked by an interface table.
Without one they stay open (`interface_unset`).

## Interface tables

A connector that implements an interface (SL23) gets its pin names from the
interface spec, not from its datasheet. The spec is then the reference for
the names:

```toml
interface    = "SL23"
spec         = "https://example.com/docs/interfaces/sl23.md"   # URL, or a path relative to this file
origin       = "manual"            # or "spec-read": names offered from the spec's table
extracted_by = "A. Person"
reviewed_by  = "A. Person"         # who checked it against the spec
reviewed_on  = "2026-11-24"

  [[variant]]                      # one per file
  part         = "SL23 SL23_Header_2x25"
  symbols      = ["Jitter_Connectors:SL23_Header_2x25"]   # or a bare symbol name
  pin_count    = 50

    [variant.pins]                 # pin number = name AS THE SPEC PRINTS IT
    1 = "IO_1"
    2 = "GND"

    [[variant.alias_for]]          # optional, as in a datasheet table
```

- **The symbol names its interface** in the field `Pinout_Spec` (here
  `Pinout_Spec = SL23`), in the symbol library. bomcheck reads it from the
  placed symbol, else from the library symbol. The field is not called
  `Interface`: the component library uses that name for a sensor's bus
  (`Interface = I2C`).
- **Keyed to symbols, not MPNs.** `symbols` lists the symbols with this
  pinout: `Lib:Name`, or `Name` for that symbol in any library. A connector
  with another pinout for the same interface (the FFC) gets its own file.
- The file lives in `reference/interface/`, in the component library or the
  board's repo, like a datasheet table. Its name is `<interface>-<symbol>.toml`.
- **Names are the spec's**, never copied from the symbol. The symbol's names
  are compared with it exactly as with a datasheet table, and differences are
  reported and settled the same way (`interface_difference`, waivable per pin).
- **Only signed files count.**
- A draft can be pre-filled from a pin table in a markdown spec. The names go
  into comments (`1 = ""   # spec: IO_1`), not into the values: only a
  person's answer fills a pin.

What `bomcheck` reports for a symbol:

| case | item |
|---|---|
| numbers-only part table, symbol names its pins, no `Pinout_Spec` field | `interface_unset` |
| `Pinout_Spec` field, no signed table for this symbol | `interface_unfilled` |
| names differ from the spec | `interface_difference` |
| table misses pins of the symbol | `interface_partial` |
| table not signed | `interface_unsigned` |

## Polarity-only parts

Diodes, LEDs, TVS diodes and polarized caps have two pins that no datasheet
numbers. Their table records the mark the datasheet shows on the part, and
the pin that mark is on:

```toml
  [[variant]]
  part   = "SMF5.0A"
  mpns   = ["SMF5.0A"]
  mark   = "band"           # plus sign | band | dot | other: <text>; optional
  marked = "cathode"        # cathode | anode | plus | minus | none | other: <text>
```

Which parts these are comes from the symbol: pins named `K`/`A`, a
description saying unidirectional, bidirectional or polarized capacitor.

bomcheck then checks, for every placement:

- the pad the placed footprint marks on silk or fab is the right symbol pin.
  A `+` sign on the footprint must be the symbol's `+` pin (pin 1 on KiCad's
  capacitor symbols). A band or bar on the footprint must be the pin that
  `marked` names, because the assembler puts the part's own mark there. A
  capacitor with `marked = "minus"` needs its band on the `-` pin. Without an
  answer: the cathode, or the `+` pin;
- a bidirectional symbol does not sit on a marked footprint, and a polar one
  not on an unmarked footprint;
- the datasheet's `marked` agrees with the symbol: `none` for a bidirectional
  symbol, not `none` for a polar one.

The footprint's mark is read from the footprint as placed in the `.kicad_pcb`.
For through-hole footprints, or when silk and fab disagree, a person confirms
it once per footprint:

```toml
[[footprint]]
name       = "Diode_SMD:D_SMA"
marked_pad = "1"            # "" = no marking
by         = "A. Person"
on         = "2026-09-30"
```

## Waivers

A design finding that is not fixed now is waived, with a reason:

```toml
[[waiver]]
code    = "polarity.bidirectional_on_marked"
refs    = ["D19"]
pins    = ["7"]         # optional, pin differences only
for_rev = "2.0"         # optional, see below
about   = "SMA6J13CA-TR; Device:D_TVS; Diode_SMD:D_SMA; bidirectional symbol on ..."
note    = "harmless; unmarked footprint in rev 3"
by      = "A. Person"
on      = "2026-09-30"
```

- `code` is the finding's stable name; `bomcheck` prints it. Waivers match on
  `code`, `refs` and `pins` only, never on a message.
- `for_rev`: the waiver holds only while the schematic's title-block revision
  is exactly this value. Any other revision fails the finding again, with a
  note that it was a known issue of the old revision: fix it, or carry it over
  on purpose (the wizard offers both). Without `for_rev`: accepted for good.
- `about` is written by the tool when you waive: what the finding was about,
  for whoever reads the file after a symbol or part number changed. Context
  only, never matched.
- `note` and `by` are required.
- Only design findings can be waived: `pin_difference`, `mixed_symbols`,
  `polarity.*`. Missing work (a stub, a signature) cannot.
- A waiver that matches no finding any more fails as stale. `bomcheck prune`
  removes all of those and lists them.

## The datasheet rule and exemptions

Every BOM line needs a datasheet in its symbol's Datasheet field: a URL, a file
in the project (`${KIPRJMOD}/datasheets/x.pdf`), or a KiCad embedded file
(`kicad-embed://x.pdf`). A path to a file that does not exist does not count.

When a part cannot have one, a person records why:

```toml
[[datasheet_exempt]]
mpn    = "SIM8051-6-0-14-01-A"
reason = "none_exists"      # confidential | too_large | none_exists | other
note   = "card holder, drawing only"   # optional
by     = "A. Person"        # required; an unsigned exemption does not count
on     = "2026-09-30"
```

An exemption excuses the Datasheet field only. The part's pin table is still
required and checked. If a PDF exists but must stay out of the repo, keep it in
your cache with `bomcheck add-pdf MPN FILE`, and the table can still be read
from it. Embedded files are the same for now: bomcheck cannot unpack them from
the schematic yet, so give it a copy the same way.

## Hand-editing

Anything the wizard does can be done in an editor: fill a stub, add an
alias line, sign. Move a finished draft to `reference/datasheet/`
(drop the `DRAFT-`/`STUB-` prefix), or run `bomcheck promote`, which refuses
unsigned or unfinished tables. A finished interface draft moves from
`reference/draft/interface/` to `reference/interface/` by hand: `promote`
handles datasheet tables only. `bomcheck` shows the result.
