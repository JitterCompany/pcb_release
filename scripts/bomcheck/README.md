# bomcheck

Checks every BOM line against its datasheet: is the symbol's pin table the one
the manufacturer prints for this MPN? Standalone for now; `pcb-release.sh` and
CI do not call it yet. The `bomfields` gate is separate: it checks that the parts
of one BOM line agree with each other, not with a datasheet.

## Requirements

- Python 3, standard library only (tested with 3.10). No pip packages.
- `kicad-cli` (tested with KiCad 10): netlist export, and the symbol and footprint
  pictures in the wizard.
- `pdftotext` and `pdftoppm` (poppler-utils): reading datasheets, and page
  pictures in the wizard. Only `prepare` and the wizard need them.
- `git`: finds the component library and the reviewer's name.
- Network: only `prepare`, `fetch` and a URL given in the wizard download
  datasheets, one at a time. A site that refuses scripts gets a request to
  download the PDF yourself and drop it on the wizard.
- `--gui`: any browser. The server is Python's own and listens on 127.0.0.1 only.

`status` and `report` need only Python, `kicad-cli` and the signed tables: no
PDFs and no network. That is what a CI gate would run.

It is self-contained in `scripts/bomcheck/` and imports nothing from the rest of
this repo, so a copy of the folder runs on its own:

    python3 scripts/bomcheck/cli.py wizard path/to/board --gui

## Using it

    bomcheck [PROJECT_DIR]           # status: what is left, and who has to do it
    bomcheck prepare [PROJECT_DIR]   # the automatic half (network, cached)
    bomcheck wizard  [PROJECT_DIR]   # answer the open items one by one
    bomcheck wizard --gui            # the same, in a browser tab
    bomcheck apply   [PROJECT_DIR]   # write BOM field answers into the schematic

(`bomcheck` = `python3 scripts/bomcheck/cli.py`. PROJECT_DIR defaults to `.`.)

`status` is offline and exits 1 while anything is open, so it is also the future
CI gate. It groups by BOM line (one MPN) and lists each open item under what has
to happen to it:

| item | who | how it closes |
|---|---|---|
| not looked at yet | tool | `prepare` fetches the datasheet and writes a draft table, or a blank stub if it cannot read one |
| give a datasheet URL | you | wizard, or `datasheet_set` in `review/decisions-fields.csv`, then `apply` |
| type the pin names | you | wizard, or fill the stub in `reference/draft/` |
| check a machine-read table | you | wizard shows it beside the symbol; you confirm or correct it against the PDF |
| pin names that differ | you | per pin: same pin (alias), table wrong (retype), symbol wrong (fix now, or waive as a known issue of this revision) |
| table misses pins | you | type the missing ones |
| sign a table | you | only a signed table counts |
| names nothing checks | you | the datasheet only numbers the pins and the symbol names them: give the symbol an `Interface` field |
| type an interface table | you | wizard shows the interface spec; you type or confirm each pin's name from it, and sign |
| names that differ from the spec | you | per pin, as for a datasheet |

In a terminal the wizard shows each answer as buttons, with datasheet, skip and
stop after them. Tab, Shift-Tab and the arrow keys move the highlight, Enter
picks, Esc or Ctrl-C stops. In a text field the arrows move the cursor until the
end of the text. Typing `:o`, `:s` or `:q` in a field still works, and so does
the bare letter of a button. Piped or non-terminal input gets plain line
prompts with the same letters. Every answer, down to a single pin name, is saved
the moment it is given, and files are written atomically, so stopping at any
point leaves nothing half-written. Run the same command again to continue.

`--gui` asks the same questions in a browser tab, with the datasheet page and
the footprint picture beside the question. It needs nothing beyond Python: a
small server on 127.0.0.1:8790. The address holds a secret, because other
users on the machine and any web page in your browser can reach 127.0.0.1; it
is made once and kept in `~/.config/bomcheck/web-token`, so the address stays
the same and can be bookmarked. Drop a PDF or a URL on the page (or paste a
URL, file:// too) to give a part its datasheet; one the script cannot download
opens in the page itself. `--no-browser` only prints the address, `--port`
picks another port.

For scripts and agents, pipe the answers in, one per line: the prompts are plain
lines then, and `:s` / `:q` skip and stop. `bomcheck` (status) exits 1 while
anything is open.

**Polarity-only parts** (diodes, LEDs, TVS, polarized caps): the wizard asks
two questions from the datasheet. First: which mark the part has (none, a +
sign, a band or bar, a dot, another mark). Second: which pin that mark is on.
The datasheet and a picture of the footprint are shown side by side. bomcheck then checks symbol,
footprint and datasheet against each other for every placement, including a
bidirectional part on a footprint with a polarity mark. See FORMAT.md.

**Connectors that implement an interface** (SL23): the connector's datasheet
only numbers its contacts, and the symbol names them after the interface spec.
Two signed tables then check the symbol:

- the part table (per MPN): "only numbers". The wizard asks this for any
  connector, also one whose symbol names its pins (or type `:n` at a pin).
  It checks the numbering, the pins joined inside the part and pin 1 against
  the footprint. It does not take over the symbol's names.
- the interface table (per symbol): pin number = signal name, from the
  interface spec. The symbol names its interface in the field
  `Interface = SL23`. The table lives in the component library, so every
  board with that symbol reuses it.

The wizard shows the spec (a URL or a file; markdown is fine) and asks each
pin's name. A pin table in a markdown spec is read as a hint: it is offered
as the answer, and you still check every name in the spec. The symbol's
names are never shown while you type. Without an `Interface` field, the
names stay open in `bomcheck`. See FORMAT.md.

**Waivers**: a design finding you will not fix now is waived in the wizard,
with a note: `k` = known issue of this revision (`for_rev`, fails again when the
schematic's revision changes), `a` = accepted for good. Waivers that no longer
match anything fail as stale; `bomcheck prune` removes them. See FORMAT.md.

**Every line needs a datasheet**: a URL, a file in the project or an embedded
file, or an exemption someone signed (`exemptions.toml`, see FORMAT.md). When
one is missing, the wizard takes whatever you paste: a URL, or a PDF path
(inside the project it is linked; outside it is copied into `datasheets/`, or
kept out of the repo as an exemption with the PDF only in your cache). `n`
records that there is none, with a reason.

**A datasheet behind a login** (Quectel): put the page you download it from in
the Datasheet field, and hand bomcheck the PDF once. The wizard asks for it when
the URL gives no PDF, or run `bomcheck add-pdf URL FILE`. The copy is filed
under that URL, so every later lookup finds it. It lives in your cache only, so
a colleague does the same once.

Datasheets are cached in `~/.cache/bomcheck` (`BOMCHECK_CACHE` overrides).
Failed downloads are remembered for 24 h, so a blocked vendor costs one timeout
a day, not one per run.

The database format is in [FORMAT.md](FORMAT.md). Signed tables can live in the
component library, so a part checked once is checked for every board that uses
it; see [Two places for signed tables](FORMAT.md#two-places-for-signed-tables).

    python3 test_tomlite.py && python3 tests/test_spike.py

One test needs real vendor PDFs, which are not in this repo. It is skipped
unless `BOMCHECK_TEST_BOARD` points at a board repo's folder that holds
`attestations/` and the board folder with its `datasheets/`.

## Current state

`bomcheck` reads a pin table from a datasheet given only the **MPN and the
document** — never the symbol — and a separate stage compares that table to the
KiCad symbol. On the first test board, 15 parts have a verified datasheet and an extracted
table, and **10 agree with the symbol on every pin read**.

    python3 cli.py report   PROJECT_DIR              # validate against promoted references
    python3 cli.py extract  PROJECT_DIR --mpn X --pdf F   # propose a pin table
    python3 cli.py fetch    PROJECT_DIR              # resolve + verify datasheets
    python3 cli.py suggest  PROJECT_DIR              # propose missing BOM fields
    python3 cli.py review   PROJECT_DIR              # -> one decisions file
    python3 cli.py apply    PROJECT_DIR [--dry-run]  # carry out ticked lines
    python3 test_tomlite.py && python3 tests/test_spike.py

| module | role |
|---|---|
| `tables.py` | the named pin-table SHAPES and the driver that applies them |
| `extract.py` | the low-level recognisers each shape is built from |
| `datasheet.py` | resolve, fetch, and VERIFY that a document is about this part |
| `ordercode.py` | decode an MPN using the datasheet's own ordering scheme |
| `pintable.py` | the evidence format, and stage-two comparison |
| `interface.py` | interface tables: spec hints, stubs |
| `suggest.py` | propose the Manufacturer / MPN / URL fields a symbol lacks |
| `netlist.py` | kicadxml reader: libpart pin tables, DNP, MPN-from-Value |
| `tomlite.py` | the TOML reader (the existing ones corrupt this data) |

## The review loop

Everything needing a human lands in two CSVs — spreadsheet work, not editing:

    bomcheck review       # -> decisions-fields.csv + decisions-pins.csv
    <open in a spreadsheet, fill or clear the `_set` cells, save>
    bomcheck apply        # add --replace to allow overwrites

**`decisions-fields.csv` — one row per part:**

| part | refs | action | mfr_now | mfr_set | datasheet_now | datasheet_set |
|---|---|---|---|---|---|---|
| MCP9701AT-E/LT | U16 | add | | Microchip Technology | https://…ok.pdf | |
| TXU0204PWR | U4 U5 | **REPLACE** | | Texas Instruments | …/txu0**3**04.pdf | …/txu0**2**04.pdf |
| SMA6J13A-TR | D3 D4 … | mixed | | | (varies) | |

**An empty `_set` cell means no change; a value means write it.** To reject a
suggestion, clear the cell — that is the whole interface.

Every field appears twice, `_now` and `_set`, so **a change to something that
already has a value is visible by construction** rather than by convention. The
`action` column names it, so a spreadsheet can sort or filter:

| action | meaning |
|---|---|
| `add` | the field is empty and a value is proposed |
| `REPLACE` | the field HAS a value and a **different** one is proposed |
| `mixed` | instances of this part disagree with each other |

A `REPLACE` needs `bom-apply --replace`. Filling an empty field is nearly free
to undo; overwriting an engineer's value is not, and the two should not be one
keystroke apart. Both REPLACEs on this board are real: U11 links
`kicad-embed://ads114s08b.pdf` — the **B variant**, a different part — and
U4/U5 link the TXU0**3**04 document.

`mixed` is a finding in its own right: `SMA6J13A-TR` carries a datasheet URL on
some of its sixteen placements and not on others.

**`decisions-pins.csv` — one row per pin whose name differs.** Here `accept`
starts empty and must be marked — the opposite default, because filling in a
missing manufacturer is housekeeping while silencing a pin-name difference is a
claim that the design is right.

### Naming conventions belong to matching, never to extraction

Stage one records what the datasheet **says**: `REFN0`, verbatim. If a house
convention leaked into extraction, the stored evidence would be our style rather
than the document, and the reference could no longer contradict the symbol —
which is the only thing it is for. Asserted, not merely intended: the suite
checks that the extraction modules contain none of the convention code, and that
the stored draft keeps the datasheet's spelling.

The matching phase then recognises conventions and reports them as **accepted**:

| the convention | why it exists |
|---|---|
| `REFN0` ≡ `REF0_N` | KiCad detects a differential pair from a `_N`/`_P` **suffix**, so a schematic that wants pairs recognised must move the polarity letter to the end |
| `PC14-OSC32_IN(PC14)` ≡ `PC14` | a datasheet names every function of a pin; a symbol names the one in use |
| `~{RESET}` ≡ `RESET_N` (also `_B`, `_L`, `RESETn`; `/RESET`, `RESET#`) | KiCad draws active-low as an overbar, datasheets often spell it with a suffix. Only when one side is the overbar form and the base names agree, so a symbol that drops the polarity (`RESET` vs `RESET_N`) is still a difference |
| `GND` ≡ `VSS` | always |
| `CLK` ≡ `SCK` ≡ `SCLK`, `DO` ≡ `SO` ≡ `SDO` ≡ `DOUT`, `DI` ≡ `SI` ≡ `SDI` ≡ `DIN`, `CS` ≡ `SS` ≡ `NSS` | only on a part the datasheet establishes as an SPI peripheral or controller; also inside multi-function names (`DO` vs `SO/IO1`) |
| `DIN` ≡ `MOSI`/`COPI`/`PICO`, `DOUT` ≡ `MISO`/`CIPO`/`POCI` (the letter after C decides: CO = controller out, CI = controller in) | a serial pin is named from the board's end — **only** where the datasheet establishes which end this part is (below) |

It tolerates a **spelling, never a pin**: base, polarity and index must all
agree, so `REFN0` vs `REFN1` and `REFN0` vs `REFP0` stay differences. They are
still listed in `decisions-pins.csv`, marked `convention (auto)` — a rule nobody
can see is a rule nobody can disagree with.

#### The SPI one carries an assumption, so it is read rather than assumed

    peripheral :  DIN = MOSI     controller:  DIN = MISO

Equating `DIN` with `MOSI` unconditionally would bless a genuinely **wrong**
symbol on any controller. So the role is read from the datasheet — a device whose
chip-select and clock are *inputs* is being selected and clocked by someone
else, which is what being a peripheral means — and stored with its evidence:

```toml
bus_role     = "peripheral"   # READ from the datasheet, not assumed
# evidence: CS is an input (9 CS Digital input); SCLK is an input (11 SCLK …)
```

| the datasheet establishes | and the symbol says `MOSI` on `DIN` |
|---|---|
| peripheral | recognised — named from the board's end |
| **controller** | **mismatch** — on a controller that pin is MISO |
| nothing | `role unknown`, put to a human |

The STM32's datasheet describes those pins in *both* directions, because an MCU
can be either — so it correctly reads as unknown. That is the case the rule
exists to not get wrong.

### What accepting a pin difference actually does

It records `symbol name == datasheet name` for that part — an **equivalence,
not a waiver**. `FB = "VFB"` means *"where this symbol says FB, read it as the
datasheet's VFB"*, and every pin is still compared against the datasheet's own
name at that position. So after accepting `FB == VFB`:

| the symbol later becomes | still reported? |
|---|---|
| pin 22 renamed to `GND` | **yes** |
| pins 21 and 22 **swapped** | **yes — both** |
| `FB` appearing on pin 23, where the datasheet says `AGND` | **yes** |

That distinction is the point. A per-pin waiver would hide exactly the defect
class this project exists to catch.

### Draft, then promoted — accepting is progress toward review, not suppression

Only `reference/datasheet/` is read by `bomcheck report`. Accepting a rename
writes into the **draft**, which nothing gates on, so it changes no verdict on
its own. It clears one obstacle to promotion:

    bomcheck promote          # draft -> reference/datasheet/, where it gates

Promotion is refused while a draft still disagrees with the board in ways nobody
has explained — that refusal is what makes promoting mean *"a human reviewed
this"* rather than *"a machine wrote this"*. On that board: **10 drafts promote
cleanly, 5 are held back**, each listing the differences blocking it.

### Surviving a spreadsheet

Only `refs`, the `_set` columns and `accept` are load-bearing; everything else
is context re-derived from the design at apply time. That is a guard, not
tidiness — spreadsheets rewrite data on save:

- **an all-numeric MPN becomes a float.** Eight parts here are affected —
  Würth's `691322310003` returns as `6.91322e+11`. Such values are written
  apostrophe-forced so a spreadsheet leaves them alone, a mangled *context*
  column changes nothing, and a mangled `_set` value is **refused**.
- a leading apostrophe (how a spreadsheet forces text) is stripped on read.
- the file is UTF-8 **with a BOM**, or Excel renders Würth as `WÃ¼rth`.

### Writing to a schematic safely

`kicad-cli` is read-only (`erc`, `export`, `upgrade`), and KiCad's IPC API is
PCB-focused with schematic coverage still to come — and needs a running KiCad,
so it cannot work headless. Edits are therefore made directly, with four rules:

- every edit is a **byte splice** into the original text; the file is never
  re-serialised, so fields this tool does not understand cannot move or be lost;
- the file must be **clean in git**, so the change is a reviewable diff;
- the result is **re-parsed and read back** before writing, so a bad splice
  fails instead of saving;
- only **instances** are touched, never the `(lib_symbols ...)` cache — editing
  that copy changes nothing on the board and diverges from the library.

One hazard is reported rather than hidden: a sheet used more than once shares a
single symbol block, so J5, J6, J7 and J8 are one edit. They are collapsed to
one line, and applying says which designators it covered.

## Two stages, and stage one never sees the symbol

    STAGE 1   (MPN, datasheet)      ->  pin map + confidence metadata
    STAGE 2   (pin map, symbol)     ->  agreements, mismatches, aliases

If the symbol could steer extraction, a symbol wired to the wrong part would
drag the evidence along with it — the exact failure this exists to catch.
Enforced by the API: `Context` carries no symbol-derived field, and the pin count
it does carry comes from the order code or the footprint's package size.

Stage two is where naming tolerance belongs, and it is deliberately narrow:
`S1_A` matches `S1A`; a symbol naming ONE function of `PC14-OSC32_IN(PC14)`
matches as a note; but `A3Y` is not a component of `A3`, so a real direction
difference still surfaces. Polarity is compared only when the reference actually
establishes it — a machine-read table does not, and asserting otherwise produced
five false mismatches on correctly-drawn active-low pins.

## What the first version set out to prove, and what it found

### 1. The defect is caught. ✅

Same MPN (`TMUX1309APWR`) in two trees, both real:

| fixture | symbol | result |
|---|---|---|
| `tmux_broken.xml` (`9a472cf~1`) | `Jitter_IC:TMUX1109` | **FAIL — 16 of 16 pins disagree** |
| `tmux_fixed.xml` (`1491adc`) | `Jitter_IC:TMUX1309A` | **PASS — 0 findings** |

Including the two pins the incident singles out: pin 8 datasheet `GND` vs symbol
`DA`, pin 14 datasheet `S1A` vs symbol `VDD`. A check that could not separate
these two trees would be worthless, so both directions are asserted.

### 2. Datasheet provenance works, and it matters. ✅

Both wrong PDFs that sat in the test board's `datasheets/` are rejected:

- `ads114s08b.pdf` → `wrong-part` for the fitted `ADS114S08IPBSR` (B variant).
  Note a substring test would PASS this, because `ADS114S08` really is inside
  `ADS114S08B`. Hence whole-token matching.
- `quectel_ec21_series_hardware_design_v2-3-1.pdf` → `wrong-part` for the
  `EG915N` line.

Controls (`BC847BPN.pdf`, `vishay_sic47x_75786.pdf`) are confirmed. 5/5 correct.

### 3. KiCad's official libraries: dropped. ❌

Measured, not estimated. Of **26 pin-mapped MPNs**, exactly **1** got a strict
match — and the board places that part *from that very library*, so the
comparison was circular. Independent references available: **0 of 26**. Naive
matching was also actively dangerous: `DMN67D8LDW-7` (dual FET, SOT-363, 6 pins)
matched `DMN67D8L` (single FET, SOT-23, 3 pins) on a name prefix.

`symbol_ref.py` was therefore **removed** (recoverable from git history if a
future board is built mostly from upstream symbols). **Datasheets are the single
source of truth.** This simplified the design rather than complicating it.

### 4. Large pin tables extract to ~90%, and the rest is a named work item. ⚠️→✅

The tedious case is the interesting one. Measured on a real 133-page module
datasheet (Quectel EC21 hardware design), pointing the tool at chapter 3.3:

    132 of 146 pins extracted, 0 conflicts
    NOT extracted: pins 4, 10, 13, 37-40, 118, 135, 137-139, 145-146

Three recognisers get there, and all three are needed:

| shape | example | why it matters |
|---|---|---|
| typed row | `S0B    1    I/O   Source pin 0…` | the normal case (TI: 16/16, matching a hand transcription exactly) |
| name + pin list | `RESERVED   18, 43, 55, 73-84, …` | one line, 20+ pins, **no type column** |
| prose range | *"GND pins 85-112 should be connected to ground"* | **40 of 146 pins** appear nowhere else in the document |

Without the last two, the same document yields 106 pins and looks finished.

The draft it writes stays *failing*: `pin_count` is declared separately from the
pins transcribed, so an incomplete table reports one `reference_incomplete`
rather than a pile of bogus per-pin findings, and `extract` exits non-zero. The
human cost is one sitting per part, once ever — point at a chapter, fill the
named gaps.

### 5. Two stages, and stage one never sees the symbol

    STAGE 1   (MPN, datasheet)      ->  pin map + confidence metadata
    STAGE 2   (pin map, symbol)     ->  agreements, mismatches, aliases

Extraction answers *"what pin map does this datasheet publish for this MPN?"*
and must be answerable without the KiCad symbol in the room. If the symbol could
steer it, a symbol wired to the wrong part would drag the evidence along with
it — which is precisely the failure this project exists to catch.

Enforced by the API, not by convention: `Context` has no symbol-derived field,
and the pin count it carries comes from the **order code** (`STM32L4R5VIT6` →
`V` → 100 pins) or, failing that, from the footprint's **package size** — a
statement about the land pattern, not about the pin map under test. Never from
the symbol's pin count. `expect_pins_source` records which was used.

The footprint's package size is deliberately conservative: `SOT-353` returns
*unknown*, because that number is a JEDEC code and not a pin count. A bogus
expectation is worse than none — it disables the over-extraction guard and
invents 348 missing pins.

Stage 2 (`pintable.compare`) is where naming tolerance belongs: `S1_A` vs `S1A`,
`~{EN}` vs `EN` marked active-low in prose, aliases recorded once and reviewed.

**One document can describe several parts.** `SCDS477A` publishes both
TMUX1308A (single 8:1) and TMUX1309A (dual 4:1) in the same TSSOP-16 with
genuinely different pinouts — pin 1 is `S4` on one and `S0B` on the other. The
MPN alone selects the table; reading the wrong one gives a complete, confident,
wrong pin map of exactly the kind that started this.

### 5. Pin tables are read by a NAMED SET OF SHAPES, not per-PDF hacks

Vendors do not invent a layout per document; they reuse a handful, and a variant
of a document you have already seen is the next one you will meet. So the
layouts are named and applied uniformly by one driver, and a new quirk is either
a new shape or a fix inside one — benefiting every part that shares it.

| shape | looks like | seen in |
|---|---|---|
| `row_per_pin` | `S0B  1  I/O  Source pin 0…` | TI TMUX1309A, ADS114S08B |
| `number_columns` | N number columns, then the name | ST STM32L4R5, Microchip MCP9701 |
| `name_then_columns` | the name, then N number columns | TI TPS7A20 |
| `name_with_pin_list` | `RESERVED  18, 43, 73-84, 140-144` | Quectel EC21 |
| `prose_ranges` | *"GND pins 85-112 should be…"* | Quectel EC21 |
| `wrapped_names` | a **repair**: reassembles a name cell split across lines | ST STM32L4R5 |

The driver is a fixed pipeline: **scope** to this part's table (a document with
one table per package will otherwise give a 5-pin part nine pins) → **apply**
shapes strongest-first, a later shape only ever ADDING pins → **validate**.

Three rules keep it from confidently inventing tables:

- a column shape finding ONE pin is ignored — one row is not a table, and
  because shapes are ordered, that single wrong pin would block the correct
  reading;
- once a column table is read, generic row scanning **stops**. Across a 308-page
  datasheet it scavenges unrelated tables: the STM32 gained 51 spurious pins on
  top of a correct 98 and the whole result was then refused;
- more pins than the part has is refused outright. A too-full table is more
  dangerous than an empty one, because it looks finished.

**The column is always chosen from the document** — from the package the MPN
decodes to, or the board footprint — never by which column best fits the symbol.
The symbol is the thing under test; fitting evidence to it would validate
anything, including the TMUX1109 that started all this.

### 6. Multi-package tables, and the MPN that decodes itself. ✅

ST publishes ONE table for every package a die ships in, headers printed
rotated, and the same package appears once per device family with different
numbering. In DS12023, `LQFP100` is column x=16 for STM32L4R5xxx/L4R7xxx and
x=115 for STM32L4R9xxx. Measured against the board's own symbol, the first
agrees on 88 of 89 pins and the second on **15 of 80** — a wrong column is a
complete, confident, wrong pin table.

The column is chosen from the DOCUMENT, never by which column best fits the
symbol: the symbol is the thing under test, and fitting evidence to it would
validate anything.

And the package need not be supplied at all — the MPN encodes it, and the
datasheet publishes the decoding scheme:

    STM32L4R5VIT6  ->  V = 100 pins,  T = LQFP   ->  LQFP100

parsed from the datasheet's own ordering-information table, so no vendor tables
are hardcoded. That also makes footprint checking free: the MPN says 100 pins,
the footprint says `LQFP-100_14x14mm`, the symbol has 100 pins — three
independent statements, cross-checked, `pin_count_mismatch` when they disagree.

Names in vertically wrapped cells are reassembled (`PC14-` / `OSC32` / <row> /
`_IN` / `(PC14)`), which took the STM32 from 89 to 96 of 100 pins. Reassembled
names are FLAGGED, because pdftotext damages them in ways an intact cell cannot
(`VDDUS` for VDDUSB) — all three damaged names fall inside the 7 flagged lines,
so a reviewer reads 7 lines instead of 100.

### 7. Vendor reachability is genuinely mixed. ⚠️

From this host, over forced HTTP/1.1: **ti.com succeeds**
(where the previous attempt failed), **st.com still times out**, Quectel gates
downloads, and **Mouser bot-blocks direct datasheet URLs** ("Access to this page
has been denied"). So the Mouser-mirror fallback is not usable as planned, and
ST/Quectel PDFs must be supplied by hand — a batched, once-per-part step. `fetch` therefore reports the whole
attempt trail — "st.com timed out" and "that archived PDF is the wrong part"
need different human actions, and on this board both happen at once.

## Design decisions worth knowing

**`tomlite.py` before anything else.** Both existing TOML readers corrupt this
board's real data while reporting success. Measured on the five prior-attempt
files, `release_ci.load_toml` recovers 15–18 strings where there are 48–573,
losing 4,130–12,939 characters each, because it does not implement
`[[array-of-tables]]` at all. It is always used in preference to `tomllib`, even
where `tomllib` exists, so CI and a laptop can never disagree about what a
database says; `tomllib` runs as a differential oracle in the tests instead.

**Evidence keyed by document + package variant.** `SCDS477A` describes *both*
TMUX1308A and TMUX1309A in the same TSSOP-16 with different pin tables — so
keying by MPN duplicates and keying by document alone is wrong.

**Membership is enumeration XOR a named decoder, never a runtime glob.**
`SMA6J13A-TR` and `SMA6J13CA-TR` differ by one character and are unidirectional
vs bidirectional TVS parts. Any pattern loose enough to be convenient merges
parts this tool exists to separate. A decoder (for passive series, where
enumeration would run to 10⁴ parts) is referenced *by name* and implemented in
this repo, so a data file can never make CI execute code it just pulled from a
data directory.

**Polarity is not in the NAME column.** TI prints `EN` in Table 5-2 and "Active
low logic input" in the description beside it; KiCad encodes that as `~{EN}`.
Transcribing only the NAME column would make the *correct* symbol look wrong —
and EN polarity was one of the real TMUX1109-vs-1309 differences. Hence
`active_low`.

**A series document is not a wrong document.** A whole-token order-code match is
`confirmed`; a document naming only the family root (`EC21`) is `family-only` and
needs a human nod. The root must appear as a WHOLE token, which is exactly what
keeps `ADS114S08B`'s datasheet rejected for the fitted `ADS114S08` part while
letting the EC21 series document through.

**Ordering-table lookups store the RESULT, not the table.** "Is this MPN in the
datasheet's ordering table?" is the strongest provenance check, but a
resistor-series document covers tens of thousands of orderable numbers. The file
records the per-MPN verdict, which is O(parts we place).

## Layout

    tomlite.py       TOML reader (+ test_tomlite.py, 20 regressions)
    netlist.py       kicadxml reader carrying <libpart> pin tables + pinmap_hash
    datasheet.py     resolve / fetch / VERIFY provenance
    pintable.py      evidence format + symbol-vs-datasheet comparison
    interface.py     interface tables: read a spec's pin table as hints
    tables.py        the named pin-table SHAPES + the driver that applies them
    extract.py       the low-level recognisers each shape is built from
    ordercode.py     decode an MPN with the datasheet's own ordering scheme
    cli.py           report | fetch | extract
    tests/           147 offline assertions + 3 netlist fixtures

Evidence files live in the BOARD repo, at
`hardware/attestations/reference/datasheet/<VENDOR>-<DOC>.toml`.
