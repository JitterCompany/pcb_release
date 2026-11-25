# bomcheck

bomcheck checks each BOM line against its datasheet. For each MPN it asks: does
the KiCad symbol carry the pin table the manufacturer prints for this part?

- The reference is a **signed table**: a TOML file with the datasheet's pin
  names, checked by a person against the PDF. The format is in
  [FORMAT.md](FORMAT.md).
- bomcheck reads a draft table from the PDF, you check and sign it, and from
  then on every board with that part is checked against it.
- It runs on its own. `pcb-release.sh` and CI do not call it yet.
- The `bomfields` gate is a different check: it compares the parts of one BOM
  line with each other, not with a datasheet.

Signed tables are an API. A board that passed must keep passing after a
bomcheck update. Read [Compatibility](FORMAT.md#compatibility-a-signed-file-is-a-promise)
before you change the format.

## Requirements

- Python 3, standard library only (tested with 3.10). No pip packages.
- `kicad-cli` (tested with KiCad 10): netlist export, and the symbol and
  footprint pictures in the wizard.
- `pdftotext` and `pdftoppm` (poppler-utils): reading datasheets, and page
  pictures in the wizard. Only `prepare` and the wizard need them.
- `git`: finds the component library and the reviewer's name.
- Network: only `prepare`, `fetch` and a URL you give the wizard download
  datasheets, one at a time.
- `--gui`: any browser.

`status` and `report` need only Python, `kicad-cli` and the signed tables. They
need no PDFs and no network, so a CI gate can run them.

The folder `scripts/bomcheck/` imports nothing from the rest of this repo. A
copy of the folder runs on its own:

    python3 scripts/bomcheck/cli.py wizard path/to/board --gui

## Using it

    bomcheck [PROJECT_DIR]           # status: what is left, and who does it
    bomcheck prepare [PROJECT_DIR]   # the automatic half (network, cached)
    bomcheck wizard  [PROJECT_DIR]   # answer the open items one by one
    bomcheck wizard --gui            # the same, in a browser tab
    bomcheck apply   [PROJECT_DIR]   # write BOM field answers into the schematic

`bomcheck` is `python3 scripts/bomcheck/cli.py` (the `bomcheck` symlink in
the folder). PROJECT_DIR is the KiCad project folder and defaults to `.`.
`bomcheck --help` lists every subcommand and option.

A normal round:

1. Run `bomcheck prepare`. It gets and verifies datasheets, reads pin tables
   into drafts, writes blank stubs for what it cannot read, and proposes
   missing BOM fields. It only works on lines that have nothing yet, so you can
   run it again at any time. `--refresh` reads unsigned machine-made tables
   again with the current extractor.
2. Run `bomcheck wizard`. Answer what it asks.
3. Run `bomcheck apply` if the wizard recorded BOM field values.
4. Run `bomcheck` until it says nothing is open.

### Status

`bomcheck` (or `bomcheck status`) is offline and exits 1 while anything is
open. It groups the open items by BOM line (one MPN) and says what has to
happen:

| item | how it closes |
|---|---|
| not looked at yet | `prepare` fetches the datasheet and writes a draft table, or a blank stub if it cannot read one |
| give a datasheet URL | wizard, or `datasheet_set` in `review/decisions-fields.csv`, then `apply` |
| type the pin names | wizard, or fill the stub in `reference/draft/` |
| check a machine-read table | wizard shows it beside the symbol. Confirm or correct it against the PDF |
| names accepted for another symbol | same pinout: add this symbol to the alias block. Otherwise confirm or type each name for this symbol |
| pin names that differ | per pin: same pin (alias), table wrong (retype), or symbol wrong (fix it, or waive it) |
| table misses pins | type the missing ones |
| sign a table | only a signed table counts |
| names nothing checks | the datasheet only numbers the pins and the symbol names them: give the symbol a `Pinout_Spec` field |
| type an interface table | wizard shows the interface spec. Type or confirm each pin's name from it, and sign |
| names that differ from the spec | per pin, as for a datasheet |

Only the first item is work for the tool. Everything else needs a person.

### The wizard

The wizard walks the open items and asks only what needs a person. Every
answer, down to a single pin name, is saved when you give it. Files are written
atomically, so you can stop at any point. Run the same command again to go on.

In a terminal:

- Each answer is a button, followed by datasheet, back, skip and stop.
- Tab, Shift-Tab and the arrow keys move the highlight. Enter picks. Esc or
  Ctrl-C stops.
- In a text field the arrows move the cursor until the end of the text.
- You can also type the commands: `:o` datasheet, `:b` back, `:s` skip part,
  `:q` stop, `-` not connected, `:n` numbers only. The bare letter of a button
  works too.

Useful options:

- `--by NAME`: your name for the signature. Default: `git user.name` of the
  board's repo.
- `--save-to library|project`: where signed tables go, for every part. Without
  it the wizard asks.
- `--library DIR` (or `BOMCHECK_LIBRARY`): which component library gets new
  tables, when the project uses more than one.
- `--no-open`: do not open datasheets by themselves.

**`--gui`** asks the same questions in a browser tab, with the datasheet page
and the footprint picture beside the question.

- A small Python server listens on 127.0.0.1:8790 (or a free port if that one
  is taken). `--port` picks another one, `--no-browser` only prints the address.
- The address holds a secret, because other users on the machine and any web
  page in your browser can reach 127.0.0.1. bomcheck makes it once and keeps it
  in `~/.config/bomcheck/web-token`, so you can bookmark the address.
- Drop a PDF or a URL on the page (or paste a URL, `file://` too) to give a
  part its datasheet.
- The terminal and the page are two front ends for the same actions. Every
  terminal command is also a button where it applies. When you add a command,
  add its button in the same change.

**For scripts**, pipe the answers in, one per line. The prompts are then plain
lines, and `:s` and `:q` skip and stop. `bomcheck` (status) exits 1 while
anything is open.

What the wizard asks for some kinds of part:

- **Polarity-only parts** (diodes, LEDs, TVS diodes, polarized caps): which mark
  the part has (none, a + sign, a band or bar, a dot, another mark), and which
  pin that mark is on. You see the datasheet and the footprint side by side.
  bomcheck then checks symbol, footprint and datasheet against each other for
  every placement.
- **Connectors that implement an interface** (SL23): the datasheet only numbers
  the contacts, and the symbol names them after the interface spec. Two signed
  tables check the symbol. The datasheet table ("numbers only") checks the
  numbering, the pins joined inside the part and pin 1 against the footprint.
  The interface table gives each pin its name from the spec. The symbol names
  its interface in the field `Pinout_Spec` (for example `Pinout_Spec = SL23`).
  The wizard shows the spec and asks each pin's name. It never shows the
  symbol's names while you type. A pin table in a markdown spec is offered as
  a hint, and you still check every name.
- **Waivers**: a design finding you do not fix now gets a waiver with a note.
  `k` = known issue of this revision (fails again when the schematic's revision
  changes). `a` = accepted for good. A waiver that matches nothing any more
  fails as stale. `bomcheck prune` removes those.
- **Every line needs a datasheet**: a URL, a file in the project, an embedded
  file, or a signed exemption in `exemptions.toml`. When one is missing, the
  wizard takes a URL or a PDF path. A PDF inside the project is linked. One
  outside is copied into `datasheets/`, or kept out of the repo as an exemption
  with the PDF only in your cache. `n` records that there is none, with a
  reason.

The details of each table are in [FORMAT.md](FORMAT.md).

### How pin names are compared

The signed table holds the datasheet's spelling. When bomcheck compares it with
the symbol, it accepts these conventions:

| convention | why |
|---|---|
| `REFN0` = `REF0_N` | KiCad finds a differential pair from a `_N`/`_P` suffix, so a schematic moves the polarity letter to the end |
| `PC14-OSC32_IN(PC14)` = `PC14` | a datasheet names every function of a pin, a symbol names the one in use |
| `~{RESET}` = `RESET_N` (also `_B`, `_L`, `RESETn`, `/RESET`, `RESET#`) | KiCad draws active-low as an overbar. Only when one side is the overbar form and the base names agree, so `RESET` vs `RESET_N` is still a difference |
| `GND` = `VSS` | always |
| `CLK` = `SCK` = `SCLK`, `DO` = `SO` = `SDO` = `DOUT`, `DI` = `SI` = `SDI` = `DIN`, `CS` = `SS` = `NSS` | only on a part the datasheet shows to be an SPI device, also inside multi-function names (`DO` vs `SO/IO1`) |
| `DIN` = `MOSI`/`COPI`/`PICO`, `DOUT` = `MISO`/`CIPO`/`POCI` | only when the datasheet shows the part is a peripheral (chip select and clock are inputs). On a controller this is a mismatch. With no evidence it goes to a person as `role unknown` |

A convention accepts a spelling, never a different pin. Base name, polarity
and index must all agree, so `REFN0` vs `REFN1` stays a difference. Pins matched
by a convention still show up in `decisions-pins.csv`, marked `auto`, so you
can disagree.

When you accept a symbol's name for a pin, bomcheck records an alias for that
pin and that exact name, not a waiver. See the alias rules in
[FORMAT.md](FORMAT.md#a-reference-file).

### Apply and the review files

`prepare` (or `bomcheck review` alone) writes two CSV files to
`attestations/review/`:

- `decisions-fields.csv`: one row per part, with proposed Manufacturer, MPN and
  Datasheet values. An empty `_set` cell means no change.
- `decisions-pins.csv`: one row per pin name that differs. Mark `accept` to
  record an alias.

Open them in a spreadsheet, edit, save as CSV, then run `bomcheck apply`. The
wizard writes its field answers into the same file, so after the wizard you
also run `apply`. The rules for editing the files are in
`review/HOW-TO-EDIT.md`, which `review` writes next to them (the text is
`HOWTO` in [decisions.py](decisions.py)).

What `apply` does:

- Writes the `_set` values into the `.kicad_sch` and records accepted pin
  names as aliases in the signed tables.
- A row that overwrites an existing value (`action` = `REPLACE`) needs
  `bomcheck apply --replace`.
- `--dry-run` shows what would change and writes nothing.
- The schematic must be clean in git, so the change is a reviewable diff.
- After it, update the layout: in the PCB editor, Tools > Update PCB from
  Schematic (F8), then save the board.

Your edits survive the next `prepare`: bomcheck carries over the decisions you
already made.

### Datasheets behind logins and blocked vendors

- **Behind a login** (Quectel): put the page you download it from in the
  Datasheet field, and give bomcheck the PDF once. The wizard asks for it when
  the URL gives no PDF. Or run `bomcheck add-pdf URL FILE`. The copy is filed
  under that URL, so every later lookup finds it.
- **Embedded or kept out of the repo**: `bomcheck add-pdf MPN FILE`. bomcheck
  cannot unpack `kicad-embed://` files from the schematic yet.
- **Blocked vendors**: some sites refuse scripted downloads, whatever the
  headers. st.com and mouser.com do, ti.com does not. bomcheck does not try to
  get round that. Download the PDF yourself and give it to bomcheck the same
  way.

The copy lives in your cache only, so a colleague does the same once.

Datasheets are cached in `~/.cache/bomcheck/datasheets` (`BOMCHECK_CACHE`
overrides, `--cache DIR` per run). A failed download is remembered for 24 h, so
a blocked vendor costs one timeout a day, not one per run.

### Where tables live

All files are in `attestations/` beside the board folder (the KiCad project's
parent folder, `<project>/../attestations`). The full layout is in
[FORMAT.md](FORMAT.md).

- Signed datasheet tables: `attestations/reference/datasheet/<MPN>.toml`.
  Characters outside `A-Za-z0-9._-` in the MPN become `_`.
- Signed interface tables: `attestations/reference/interface/<interface>-<symbol>.toml`.
- Drafts and stubs: `attestations/reference/draft/` (`DRAFT-<MPN>.toml`,
  `STUB-<MPN>.toml`, interface drafts in `draft/interface/`). Promote a
  finished draft in the wizard, with `bomcheck promote`, or by moving it by
  hand.
- Working files: `attestations/review/`. bomcheck regenerates them and keeps
  them out of git.

A signed table can also live in the component library, so a part checked once
is checked on every board that uses it. See
[Two places for signed tables](FORMAT.md#two-places-for-signed-tables).

### Other subcommands

`bomcheck --help` has the full list. These are for debugging or for single
steps of `prepare`:

| command | what it does |
|---|---|
| `report` | the full check as a markdown report, against signed tables only |
| `fetch` | find, download and verify datasheets, and list what could not be got |
| `extract --mpn X --pdf F` | read a pin table from one PDF into a draft |
| `suggest` | propose missing Manufacturer, MPN and Datasheet fields |
| `review` | write the two review CSV files |
| `promote` | move signed drafts to `reference/datasheet/`. Refuses unexplained differences unless `--force` |
| `stub` | a blank table to type by hand: pin numbers filled in, names empty |
| `prune` | remove stale waivers |

### Running the tests

From `scripts/bomcheck/`:

    python3 tests/test_tomlite.py && python3 tests/test_bomcheck.py

- Both are offline. They use the page text and netlists in `tests/fixtures/`.
- Each datasheet layout the reader handles has a test, and its docstring names
  the vendor quirk.
- A few tests need real vendor PDFs, which are not in this repo. They are
  skipped unless `BOMCHECK_TEST_BOARD` points at a folder that holds
  `attestations/` and the board folder with its `datasheets/`.
- `tests/fixtures/compat/` is frozen. See
  [Compatibility](FORMAT.md#compatibility-a-signed-file-is-a-promise).

## Limits

- Not in CI or `pcb-release.sh` yet.
- bomcheck cannot read `kicad-embed://` datasheets. Give it a copy with
  `add-pdf`.
- There is no distributor fallback for datasheets. `datasheet.resolve()` takes
  a mirror URL, but no command passes one. A vendor that blocks scripts needs a
  PDF from you.
- Machine reading is incomplete for large module tables. A draft that misses
  pins stays open (`pin_count` above the pins listed) until you type the rest.
- A machine-read table does not know which pins are active-low. Polarity is
  only compared when the table says so (`active_low`).
- A sheet used more than once shares one symbol block. A field edit on it
  changes every designator it serves. `apply` says which ones it covered.
- `apply` needs a clean schematic in git, and the layout needs an update from
  the schematic afterwards.

## Why it works this way

**Stage one reads the datasheet without ever seeing the symbol.** Extraction
gets only the MPN and the document (`tables.Context` has no field from the
symbol). If the symbol could steer it, a symbol drawn for the wrong part would
pull the evidence along with it. The expected pin count comes from the order
code or the footprint's package size, never from the symbol. A package size
that is not a pin count (`SOT-353`) gives "unknown", because a wrong
expectation is worse than none.

**Names are the datasheet's, never copied from the symbol.** A table copied
from the symbol would agree with the thing it is there to check. Stubs leave
names blank for the same reason. Naming conventions belong to the comparison,
not to extraction, and a test checks that the extraction modules contain no
convention code.

**The column comes from the document.** One table often covers several
packages. bomcheck picks the column from the package the MPN decodes to (from
the datasheet's own ordering table) or from the footprint. It never picks the
column that fits the symbol best: that would make any symbol pass.

**Signed tables are an API.** A released board must keep passing when bomcheck
changes, so the format only grows. See
[Compatibility](FORMAT.md#compatibility-a-signed-file-is-a-promise).

**tomlite instead of a TOML library.** The tables are the database, so a
misread is a silent wrong result. Small ad-hoc readers that split on `,` or
`#` break on quoted strings, which these files are full of. `tomllib` is not in
Python 3.10, and picking a parser by Python version would let CI and a laptop
read the same file differently. So bomcheck always uses `tomlite.py`, and the
tests compare it with `tomllib` where that exists.

**One document can describe several parts.** A datasheet often covers a family
with different pinouts in the same package. So a table is one document with one
`[[variant]]` per part and package, and the MPN selects the variant. Reading
the wrong variant gives a complete, confident, wrong pin table.

**A datasheet must be about this part.** A file that is present proves nothing.
bomcheck matches the order code as a whole token (`ADS114S08` is not
`ADS114S08B`). A document that names only the family is `family-only` and needs
a person. A document that names another part far more often than ours is
rejected.

**MPNs are listed, never matched by pattern.** Two parts can differ by one
character and still differ in function (a uni- and a bidirectional TVS diode).
A table lists its MPNs exactly, or names a decoder implemented in this folder,
so a data file can never make CI run code.

**Pin tables are read by a named set of shapes.** Vendors reuse a few layouts,
so `tables.py` names them and one driver applies them. A new quirk is a new
shape or a fix inside one, and every part that shares the layout benefits. A
table with more pins than the part has is refused, because it looks finished.

**Datasheets are the only reference.** KiCad's own symbol libraries were tried
as a second source. They matched almost no MPNs independently, and a name
prefix match joined different parts. So bomcheck uses no symbol library as
evidence.

**Numbers-only parts and interface tables.** A connector's datasheet numbers
its contacts and names none of them. Its table claims only the numbering. When
the symbol names the pins after an interface (SL23), the names come from the
interface spec, in an interface table keyed to the symbol, not the MPN. So the
same spec checks every connector symbol with that pinout.

**Overwriting needs a separate flag.** Filling an empty field is easy to undo.
Overwriting an engineer's value is not, so `REPLACE` rows need `--replace`.
Accepting a pin difference is a claim about the design, so `accept` starts
empty.

## Modules

| module | role |
|---|---|
| `atomic.py` | write a file so an interrupted write leaves the old one intact |
| `cli.py` | the command line: every subcommand, project discovery, netlist export |
| `datasheet.py` | find, download and verify a datasheet for an MPN, with the cache |
| `decisions.py` | the review CSV files, their HOW-TO-EDIT text, and alias writing |
| `exempt.py` | exemptions from the datasheet rule (`exemptions.toml`) |
| `extract.py` | the low-level recognisers the table shapes are built from |
| `footprint.py` | which pad a two-pad footprint marks, and a picture of it |
| `interface.py` | interface tables: spec hints and stubs |
| `library.py` | where signed tables live: the board's repo and the component library |
| `netlist.py` | read a `kicadxml` netlist with the symbol pin tables, DNP and MPN-from-Value |
| `ordercode.py` | decode an MPN with the datasheet's own ordering table |
| `pintable.py` | the signed table format, and the comparison with the symbol |
| `polarity.py` | two-pin parts where only the polarity can be checked |
| `reffile.py` | edit a table in place as text, so comments stay |
| `schematic.py` | read and write symbol fields in a `.kicad_sch` by byte splices |
| `suggest.py` | propose the Manufacturer, MPN and Datasheet fields a symbol lacks |
| `symgraph.py` | what a symbol's drawing says beyond pin names: units, joined pins, dots |
| `tables.py` | the named pin-table shapes, and the driver that applies them |
| `todo.py` | what each BOM line still needs, and from whom (`status`, wizard) |
| `tomlite.py` | the TOML reader |
| `tui.py` | the terminal prompt: a text field and a row of buttons |
| `waivers.py` | accepted design findings (`waivers.toml`) |
| `webui.py`, `webui.html` | the wizard in a browser tab |
| `wizard.py` | walk the open items, one BOM line at a time |
| `tests/` | `test_bomcheck.py`, `test_tomlite.py` and their fixtures |
