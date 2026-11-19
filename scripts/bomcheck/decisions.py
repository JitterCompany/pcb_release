#!/usr/bin/env python3
"""The review files: one row per part, edited in a spreadsheet.

Findings that are only *reported* stay unfixed, so everything needing a human
lands in two CSVs, and `bomcheck apply` carries out what is written in them.

FIELDS -- `decisions-fields.csv`, one row per part:

    part            refs     action   mfr_now  mfr_set               note
    MCP9701AT-E/LT  U16      add               Microchip Technology
    TXU0204PWR      U4 U5    REPLACE  TI       Texas Instruments     linked...
    691305330003    J5 J6..  -        Würth

**An empty `_set` cell means no change; a value means set the field to it.**
That is the whole interface: to reject a suggestion, clear the cell.

Every field appears TWICE -- `_now` and `_set` -- so a change to something that
already has a value is visible by construction rather than by convention. The
`action` column names it so a spreadsheet can sort or filter on it:

    add       the field is empty and a value is proposed
    REPLACE   the field HAS a value and a different one is proposed
    mixed     instances of this part disagree with each other, which is a
              finding in itself -- SMA6J13A-TR carries a datasheet URL on some
              of its sixteen placements and not on others

A REPLACE is not carried out unless `bomcheck apply --replace` is passed.
Filling an empty field is nearly free to undo; overwriting an engineer's value
is not, and the two should not be one keystroke apart.

PINS -- `decisions-pins.csv`, one row per pin whose name differs. Here the
`accept` column starts EMPTY and must be marked, the opposite default: adding a
missing manufacturer is housekeeping, while silencing a pin-name difference is a
claim that the design is right, and that should take a deliberate act.

SURVIVING A SPREADSHEET. Only `refs`, the `_set` columns and `accept` are
load-bearing; everything else is context re-derived from the design at apply
time. Spreadsheets rewrite data on save, and eight parts on this board have
all-numeric MPNs -- Würth's 691322310003 comes back as 6.91322e+11 -- so long
digit strings are written apostrophe-forced, a mangled context column changes
nothing, and a mangled `_set` value is refused rather than written.
"""
import csv
import io
import os
import re

import atomic

FIELD_KEYS = [("mfr", "Manufacturer"), ("datasheet", "Datasheet"),
              ("mpn", "MPN")]
FIELD_COLS = (["part", "refs", "action"]
              + [c for k, _ in FIELD_KEYS for c in (f"{k}_now", f"{k}_set")]
              + ["note"])
PIN_COLS = ["accept", "part", "ref", "pin", "symbol_says", "datasheet_says",
            "verdict", "note"]

# What a spreadsheet turns 691322310003 into. Never write this to a design.
_MANGLED = re.compile(r"^[+-]?\d(?:\.\d+)?[Ee][+-]?\d+$")
_MIXED = "(varies)"


def _unforce(v):
    """Drop the apostrophe a spreadsheet adds to keep a cell as text."""
    v = (v or "").strip()
    return v[1:] if v.startswith("'") and len(v) > 1 else v


def _force(v):
    """Keep a long digit string as text so a spreadsheet cannot reformat it."""
    v = str(v or "")
    return "'" + v if v.isdigit() and len(v) > 8 else v


class FieldEdit:
    __slots__ = ("part", "refs", "field", "value", "current", "action", "row_no",
                 "problem")

    def __init__(self, part, refs, field, value, current, action, row_no):
        self.part, self.refs, self.field = part, refs, field
        self.value, self.current, self.action = value, current, action
        self.row_no, self.problem = row_no, ""
        if _MANGLED.match(value):
            self.problem = (f"row {row_no}: {field} value {value!r} looks like a "
                            f"number a spreadsheet reformatted -- refusing to "
                            f"write it. Re-type it as text, or regenerate.")

    @property
    def is_replace(self):
        return bool(self.current) and self.current != _MIXED

    def __repr__(self):
        return f"<{'/'.join(self.refs)}.{self.field}={self.value!r} {self.action}>"


class PinEdit:
    __slots__ = ("part", "ref", "pin", "symbol", "datasheet", "accepted",
                 "auto", "row_no")

    def __init__(self, part, ref, pin, symbol, datasheet, accepted, row_no,
                 auto=False):
        self.part, self.ref, self.pin = part, ref, pin
        self.symbol, self.datasheet = symbol, datasheet
        self.accepted, self.row_no = accepted, row_no
        # Marked `auto`: a naming convention the tool already recognises at
        # match time. Listed so it can be disagreed with, but recording it as an
        # alias would write a rule the tool already applies into every file that
        # happens to use it -- noise that also hides which rows a human decided.
        self.auto = auto

    def __repr__(self):
        return (f"<{self.ref} pin {self.pin} {self.symbol}=={self.datasheet} "
                f"{'x' if self.accepted else ' '}>")


def field_row(part, refs, currents, proposals, note=""):
    """currents/proposals: {kicad field name: value}. -> one CSV row dict."""
    row = {"part": part, "refs": " ".join(refs), "note": note}
    actions = []
    for key, name in FIELD_KEYS:
        cur = currents.get(name, "")
        new = proposals.get(name, "")
        row[f"{key}_now"] = _force(cur)
        row[f"{key}_set"] = _force(new)
        # A field whose instances DISAGREE is a finding whether or not anything
        # is proposed for it, so it names the action on its own.
        if cur == _MIXED:
            actions.append("mixed")
        elif new:
            actions.append("REPLACE" if cur else "add")
    order = {"REPLACE": 0, "mixed": 1, "add": 2}
    row["action"] = (sorted(set(actions), key=lambda a: order[a])[0]
                     if actions else "-")
    return row


def pin_row(part, ref, pin, symbol, datasheet, verdict, note="", accept=""):
    return {"accept": accept, "part": part, "ref": ref, "pin": pin,
            "symbol_says": symbol, "datasheet_says": datasheet,
            "verdict": verdict, "note": note}


def carry_over(path, rows, key_cols, carry_cols):
    """Preserve a human's edits when the file is regenerated.

    Regenerating used to overwrite the file, so every mark made since the last
    run was silently destroyed -- which turns "run review again to pick up your
    symbol fix" into "lose an afternoon of decisions". Rows are matched on the
    columns that identify them (part and field, or part/ref/pin); anything the
    person typed in `carry_cols` wins over what was proposed, because they may
    have CORRECTED it rather than merely accepted it.

    Rows that no longer exist simply vanish, which is right: the finding they
    recorded a decision about is gone.
    """
    if not os.path.isfile(path):
        return rows, 0
    old = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            old[tuple((r.get(k) or "").strip() for k in key_cols)] = r
    carried = 0
    for row in rows:
        prev = old.get(tuple(str(row.get(k, "")).strip() for k in key_cols))
        if not prev:
            continue
        for c in carry_cols:
            was = (prev.get(c) or "").strip()
            if was and was != str(row.get(c, "")).strip():
                row[c] = was
                carried += 1
    return rows, carried


def write(path, rows, cols):
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    atomic.write_text(path, buf.getvalue(), encoding="utf-8-sig")


def parse_fields(path):
    """-> [FieldEdit] for every non-empty `_set` cell."""
    out = []
    if not os.path.isfile(path):
        return out
    with open(path, newline="", encoding="utf-8-sig") as f:
        for n, row in enumerate(csv.DictReader(f), 2):
            refs = [r for r in (row.get("refs") or "").replace(",", " ").split()
                    if r]
            if not refs:
                continue
            for key, name in FIELD_KEYS:
                val = _unforce(row.get(f"{key}_set", ""))
                if not val:
                    continue
                out.append(FieldEdit(_unforce(row.get("part", "")), refs, name,
                                     val, _unforce(row.get(f"{key}_now", "")),
                                     (row.get("action") or "").strip(), n))
    return out


def parse_pins(path):
    """-> [PinEdit]; only rows with something in `accept` are accepted."""
    out = []
    if not os.path.isfile(path):
        return out
    with open(path, newline="", encoding="utf-8-sig") as f:
        for n, row in enumerate(csv.DictReader(f), 2):
            ref = (row.get("ref") or "").strip()
            if not ref:
                continue
            mark = (row.get("accept") or "").strip()
            out.append(PinEdit(_unforce(row.get("part", "")), ref,
                               (row.get("pin") or "").strip(),
                               _unforce(row.get("symbol_says", "")),
                               _unforce(row.get("datasheet_says", "")),
                               bool(mark), n, auto=mark.lower() == "auto"))
    return out


HOWTO = """# How to edit these two files

Both are CSV. Open them in a spreadsheet, change the cells described below,
save as CSV, then run `bomcheck apply`.

Nothing is written to your design until you run that.

---

## decisions-fields.csv — there is NO tick column, on purpose

**The `_set` cell IS the decision.**

| to do this | do this |
|---|---|
| accept a proposed value | leave the `_set` cell as it is |
| correct it | type your value over it |
| reject it | **clear the cell** |

Every field appears twice. `mfr_now` is what your schematic says today;
`mfr_set` is what will be written. `_now` is read-only context -- editing it
does nothing.

The `action` column tells you what kind of change it is:

| action | meaning |
|---|---|
| `add` | the field is empty on some or all placements; filling it in |
| `REPLACE` | it already has a DIFFERENT value. Needs `bomcheck apply --replace`, because overwriting your value should not be one keystroke |
| `mixed` | placements of this part disagree with each other and nothing is proposed -- resolve it by hand |

## decisions-pins.csv — this one DOES have a tick column

Here the `accept` column starts empty and you put something in it: `x`, `y`,
`yes`, anything. Blank means "not accepted".

The asymmetry is deliberate. Filling in a missing manufacturer is
housekeeping; saying a pin-name difference is fine is a claim about the
design, so it takes a positive act.

Rows already marked `auto` are naming conventions the tool recognised (`REFN0`
vs `REF0_N`, and `DIN` vs `MOSI` where the datasheet establishes the part is an
SPI peripheral, GND vs VSS). They are listed so you can disagree -- clear the cell to do so.

## Both files

* Your edits SURVIVE a re-run of `bomcheck prepare`; rows are matched and your
  entries win over freshly proposed ones.
* Long all-digit values are written with a leading apostrophe so a spreadsheet
  cannot turn `691322310003` into `6.91322e+11`. It is stripped when read.
* Only `refs`, the `_set` columns and `accept` are load-bearing -- if a
  spreadsheet mangles a context column, nothing breaks.
"""


def write_howto(path):
    atomic.write_text(path, HOWTO)


def add_alias(ref_path, mpn, pin, sym_name, ds_name):
    """Accept the symbol's name for one pin, in the reference file.

    Appended as text rather than by re-rendering the file, so a reviewed pin
    table is never rewritten by a machine as a side effect of accepting a name.
    """
    text = open(ref_path, encoding="utf-8").read()
    key = pin if re.fullmatch(r"[A-Za-z0-9_-]+", pin) else '"' + pin + '"'
    value = '"' + sym_name.replace("\\", "\\\\").replace('"', '\\"') + '"'
    entry = f"    {key} = {value}   # datasheet: {ds_name}"
    head = "[variant.symbol_alias]"
    rx = re.compile(rf"^\s*{re.escape(key)}\s*=.*$", re.M)
    if head in text:
        at = text.index(head)
        eol = text.index("\n", at) + 1
        nxt = re.search(r"^\s*\[", text[eol:], re.M)
        end = eol + nxt.start() if nxt else len(text)
        block = text[eol:end]
        if rx.search(block):
            block = rx.sub(lambda _m: entry, block, count=1)
        else:
            block = block.rstrip("\n") + ("\n" if block.strip() else "") + entry + "\n"
        text = text[:eol] + block + text[end:]
    else:
        text = text.rstrip("\n") + f"\n\n    {head}\n" + entry + "\n"
    atomic.write_text(ref_path, text)
    return True


def set_field(path, part, refs, name, value, current=""):
    """Set one `_set` cell, adding the part's row if it has none."""
    key = next(k for k, n in FIELD_KEYS if n == name)
    rows = []
    if os.path.isfile(path):
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    row = next((r for r in rows if _unforce(r.get("part", "")) == part), None)
    if row is None:
        row = field_row(part, refs, {name: current}, {})
        rows.append(row)
    row[f"{key}_set"] = _force(value)
    row[f"{key}_now"] = row.get(f"{key}_now") or _force(current)
    if row.get("action") in ("", "-", None):
        row["action"] = "REPLACE" if current else "add"
    write(path, rows, FIELD_COLS)
