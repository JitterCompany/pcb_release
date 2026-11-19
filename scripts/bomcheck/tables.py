#!/usr/bin/env python3
"""The pin-table SHAPES a datasheet can use, as a named, reusable set.

WHY THIS MODULE EXISTS. Pin-table reading grew one special case at a time, and
that is the wrong shape for the problem: every quirk learned from one PDF was
buried in a regex somewhere and helped no other part. Vendors do not invent a
new layout per document -- they reuse a handful, and a variant of a document you
have already seen is the NEXT document you will see. So the layouts are named,
documented, and applied uniformly, and a new quirk is either a new SHAPE or a
fix inside one, benefiting every part that shares it.

THE SHAPES, all observed in real datasheets in this project:

  row_per_pin            one pin per row, in either order
                         TI TMUX1309A:  S0B  1  I/O  Source pin 0...
                         TI ADS114S08B:  1  AINCOM  Analog input  Common...

  number_columns         N pin-number columns, THEN the name. One column per
                         package, and the same package may appear once per
                         device family with different numbering.
                         ST STM32L4R5:  23 L2 L2 34 ... K3  PA0  I/O  FT_a
                         Microchip MCP9701:  1  -  -  NC  No Connect...

  name_then_columns      the name FIRST, then N pin-number columns.
                         TI TPS7A20:  IN  4  1  I   (X2SON=4, SOT-23=1)

  header_table           one number and one name column, found by the heading
                         and read by position, whatever the type cell says.
                         Renesas SLG59M301V:  Pin #  Pin Name  Type  ...
                                                3, 4     D      MOSFET

  pinout_diagram         a dual-row drawing, each row's numbers summing to N+1.
                         Infineon S25FL064L:  CS#   1      8   VCC

  name_with_pin_list     one name owning a list of pins, no type column.
                         Quectel EC21:  RESERVED  18, 43, 55, 73-84, 140-144

  prose_ranges           pins named only in body text.
                         Quectel EC21:  "GND pins 85-112 should be connected"

  wrapped_names          a REPAIR, not a layout: reassembles a name cell that
                         pdftotext split across lines because the PDF centres it
                         vertically on its row. Applies after number_columns.

Each shape reports which pins it found, so a merged table carries provenance and
disagreements are visible instead of silently resolved.
"""
import re

import extract as E


# Shapes that read a table of columns. One row is never a table.
_COLUMN_SHAPES = ("number_columns", "name_then_columns")
# Shapes that read one identified table rather than scanning for rows. Once one
# of them has read nearly the whole part, the scavengers stay out.
_STRUCTURED = _COLUMN_SHAPES + ("header_table", "pinout_diagram")

# Generic scavengers: they scan the whole document for anything row-shaped. That
# is right when nothing structured was found, and wrong once a column table has
# been read -- across a 308-page datasheet they collect rows from unrelated
# tables, and the STM32 acquired 51 spurious pins on top of a correct 98,
# tipping the result past the over-extraction guard and losing it entirely.
_SCAVENGERS = ("row_per_pin", "number_name_only", "name_with_pin_list",
               "prose_ranges")


class Result:
    """What one shape found on one scope, with provenance."""

    __slots__ = ("shape", "pins", "note", "low_confidence")

    def __init__(self, shape, pins, note="", low_confidence=()):
        self.shape = shape
        self.pins = pins                    # {pin_number: name}
        self.note = note
        # Pins whose NAME was reconstructed rather than read intact. They are
        # real findings, but damaged in ways an intact cell cannot be, so they
        # are marked for review rather than trusted.
        self.low_confidence = set(low_confidence)


class Context:
    """What STAGE ONE is allowed to know: the part, and nothing about the symbol.

    Extraction answers one question -- "what pin map does this datasheet publish
    for this MPN?" -- and it must be answerable without ever seeing the KiCad
    symbol. Otherwise the reference is fitted to the thing it is supposed to
    check, and a symbol wired to the wrong part would quietly drag the evidence
    along with it. That is the exact failure this project exists to catch.

    So NOTHING derived from the symbol enters here. In particular `expect_pins`
    comes from the ORDER CODE (STM32L4R5VIT6 -> V -> 100 pins) or, failing that,
    from the footprint NAME's package size -- a statement about the PACKAGE, not
    about the pin map under test -- and never from the symbol's pin count.
    `expect_pins_source` records which, so a report can say how it was decided.

    The package hint is likewise a statement about which physical part is
    fitted. It selects WHICH table to read; it never influences what that table
    is read to say.
    """

    __slots__ = ("mpn", "family", "package", "footprint", "expect_pins",
                 "expect_pins_source", "exposed_pads")

    # Packages whose pins are named by row+column rather than numbered.
    _ALNUM_PKG = ("BGA", "CSP", "WLCSP", "UFBGA", "TFBGA", "LFBGA", "UBGA",
                  "DSBGA", "LGA")

    def __init__(self, mpn="", family="", package="", footprint="",
                 expect_pins=0, expect_pins_source="", exposed_pads=None):
        self.mpn, self.family = mpn, family
        self.package, self.footprint = package, footprint
        self.expect_pins = int(expect_pins or 0)
        self.expect_pins_source = expect_pins_source or (
            "caller" if expect_pins else "unknown")
        self.exposed_pads = (self.footprint_exposed_pads(footprint)
                             if exposed_pads is None else exposed_pads)

    @staticmethod
    def footprint_exposed_pads(footprint):
        """Exposed pads the land pattern adds after the leads: VSSOP-8-1EP -> 1.

        KiCad numbers such a pad N+1, and a datasheet lists it without a
        number ("Thermal pad  --"), so the pad count is what lets its row be
        placed at all. Read from the footprint NAME, like the lead count."""
        name = str(footprint).partition(":")[2] or str(footprint)
        m = re.search(r"-(\d)EP(?![a-z])", name)
        return int(m.group(1)) if m else 0

    @property
    def total_pins(self):
        """Leads plus exposed pads, or 0 when the lead count is unknown."""
        return self.expect_pins + self.exposed_pads if self.expect_pins else 0

    # Package families whose name is followed by the PIN COUNT. Deliberately a
    # whitelist: SOT and SC are excluded because their numbers are JEDEC codes,
    # not pin counts -- "SOT-353" would otherwise claim a 353-pin part, which is
    # far worse than admitting the count is unknown, because a bogus expectation
    # disables the over-extraction guard and invents 348 missing pins.
    _COUNTED_PKG = ("LQFP", "TQFP", "PQFP", "HTQFP", "QFP", "TSSOP", "HTSSOP",
                    "SSOP", "VSSOP", "MSOP", "SOIC", "QFN", "VQFN", "UQFN",
                    "WQFN", "DFN", "WSON", "SON", "LGA", "BGA", "UFBGA",
                    "TFBGA", "LFBGA", "WLCSP", "DIP", "TSOP")

    @staticmethod
    def package_pin_count(footprint):
        """Pins implied by a footprint NAME, e.g. LQFP-100_14x14mm -> 100.

        A property of the land pattern, independent of what the symbol claims
        the pins are called -- which is why it is an acceptable fallback when
        the order code cannot be decoded. Returns 0 when it cannot tell, and
        0 is a supported answer: completeness simply goes unchecked.
        """
        name = str(footprint).partition(":")[2] or str(footprint)

        # JEDEC-style dimensional names put the BODY SIZE straight after the
        # family and the pin count at the end: DFN200X150X80-8N is an 8-pin
        # part, not a 200-pin one. Detected by the family being followed
        # immediately by a digitsXdigits group, which "LQFP-100_14x14mm" is not.
        dim = re.search(r"([A-Za-z]{2,10})(\d+X\d+)", name, re.I)
        if dim:
            tail = re.search(r"-(\d{1,3})[A-Za-z]?$", name)
            n = int(tail.group(1)) if tail else 0
            return n if 2 <= n <= 600 else 0

        # A JEDEC code with the lead count after it: SOT-23-5, SC-70-6. The
        # code alone (SOT-353) is not a count and stays unread.
        hits = {int(m.group(1)) for m in re.finditer(
            r"(?:SOT|SC)-?\d{2,3}-(\d{1,2})(?![0-9x.])", name, re.I)}
        for fam in Context._COUNTED_PKG:
            for m in re.finditer(rf"{fam}[-_]?(\d{{1,3}})(?![0-9])", name,
                                 re.I):
                n = int(m.group(1))
                if 2 <= n <= 600:
                    hits.add(n)
        # Two different answers means the name is ambiguous, not that one of
        # them is right.
        return hits.pop() if len(hits) == 1 else 0

    @property
    def numeric_pins(self):
        """True when this package numbers its pins 1..N.

        Derived from the PACKAGE, which is known independently of the symbol --
        so using it to reject a reading is not circular. It catches a whole
        class of misreads at once: TXU0204's table has logic names like A1 and
        B4 in its first column, and without this they are accepted as pin
        NUMBERS, producing entries like {"B4Y": "A4"}.
        """
        blob = f"{self.package} {self.footprint}".upper()
        return not any(p in blob for p in self._ALNUM_PKG)

    @property
    def package_hint(self):
        """What identifies the fitted package, best first.

        The KiCad library prefix is dropped: "Package_SO:TSSOP-14_..." contains
        the literal word PACKAGE, which matches a column header called
        "PACKAGE" and selects the wrong table.
        """
        if self.package:
            return self.package
        fp = self.footprint or ""
        return fp.partition(":")[2] or fp


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------

def shape_row_per_pin(text, ctx):
    r = E.extract(text, None, 0, use_prose=False)
    return Result("row_per_pin", r["pins"])


def shape_name_with_pin_list(text, ctx):
    pins = {}
    for page_no, page in enumerate(text.split("\f"), 1):
        for pin, name, how, _ in E.recognise_rows(page, page_no):
            if how == "row:name+numlist":
                pins[pin] = name
    return Result("name_with_pin_list", pins)


def shape_prose_ranges(text, ctx):
    pins = {}
    for page_no, page in enumerate(text.split("\f"), 1):
        for pin, name, _how, _ in E.recognise_prose(page, page_no):
            pins.setdefault(pin, name)
    return Result("prose_ranges", pins,
                  note="named only in body text; confirm against the drawing")


def shape_number_columns(text, ctx):
    if not ctx.package_hint:
        return Result("number_columns", {},
                      note="needs a package to pick a column")
    pins, meta = E.extract_multicolumn(text, ctx.family or ctx.mpn[:8],
                                       ctx.package_hint)
    return Result("number_columns", pins, note=meta.get("why", ""),
                  low_confidence=meta.get("wrapped_pins") or ())


_TYPE_CELL = re.compile(r"I/O|IO|I|O|S|P|PI|PO|NC|N/C|PWR|GND|-|—|"
                        r"Input|Output|Power|Ground|Supply|Passive",
                        re.I)
_NAME_CELL = re.compile(r"[A-Z][A-Z0-9_+\-/.]{0,20}")
_NUM_CELL = re.compile(r"[0-9]{1,3}|[A-Z]{1,2}[0-9]{1,2}|-|—")
_NUM_LIST_CELL = re.compile(r"\d{1,3}(?:\s*,\s*\d{1,3})+")


def shape_name_then_columns(text, ctx):
    """NAME first, then one pin-number column per package, then a type.

        NAME          X2SON           SOT-23
        IN              4                1          I
        OUT             1                5          O

    The mirror image of number_columns, and just as unforgiving: choosing the
    X2SON column for a SOT-23 part gives a complete, confident, wrong table.
    The column is picked from the header row by matching the package hint, never
    by which column looks more plausible.
    """
    hint = re.sub(r"[^A-Z0-9]", "", (ctx.package_hint or "").upper())
    if not hint:
        return Result("name_then_columns", {},
                      note="needs a package to pick a column")

    pins, why = {}, ""
    for page in text.split("\f"):
        lines = page.split("\n")
        # Collect EVERY plausible header line, not just the first. The section
        # heading "Pin Functions: X2SON, SOT-23" names the same packages as the
        # table header below it, so taking the first match reads the column
        # offset off the heading and finds no data under it.
        candidates = []
        for ln in lines:
            if E._SECTION_HEAD.match(ln):
                continue
            cells = [(m.start(), m.group(0)) for m in re.finditer(r"\S+", ln)]
            if len(cells) < 3:
                continue
            # The heading of a name-first table starts with the name column.
            # Without this any line mentioning the part and a package -- a
            # footer, an ordering row -- was taken for a heading.
            if not _HEAD_NAME.fullmatch(cells[0][1]):
                continue
            heads = []
            for x, t in cells[1:]:
                tok = t.strip().strip(",;/()")
                if len(tok) >= 2 and re.fullmatch(r"[A-Z][A-Z0-9\-]{1,10}", tok):
                    heads.append((x, tok))
            if len(heads) < 2:
                continue
            mpn_n = re.sub(r"[^A-Z0-9]", "", (ctx.mpn or "").upper())
            norm = lambda t: re.sub(r"[^A-Z0-9]", "", t.upper())
            by_pkg = [(x, t) for x, t in heads
                      if len(norm(t)) >= 3 and (norm(t) in hint or hint in norm(t))]
            by_mpn = [(x, t) for x, t in heads
                      if len(norm(t)) >= 2 and norm(t) in mpn_n]
            # Uniqueness matters more than eagerness: two candidates mean the
            # header did not identify the part, and guessing gives a complete
            # wrong table rather than an obvious failure.
            if len(by_pkg) > 1:
                by_pkg = []
            pick = by_pkg or (by_mpn if len(by_mpn) == 1 else [])
            if pick:
                candidates.append((heads, pick[0]))

        # Use the column's ORDINAL POSITION, not its x offset. Header text is
        # not aligned with the data beneath it -- TI's "PW, BQA" label starts
        # eight characters left of the numbers it heads -- and one label may
        # name several packages, so the tokens must first be clustered into
        # columns by proximity ("PW"@19 and "BQA"@23 are one column; "RUT"@41
        # and "DTR"@46 are the next).
        for heads, pick in candidates:
            groups = []
            for x, tok in heads:
                if groups and x - groups[-1][-1][0] <= 10:
                    groups[-1].append((x, tok))
                else:
                    groups.append([(x, tok)])
            # After clustering, the header may collapse to ONE column -- the
            # tokens were a single label all along. That is not a multi-package
            # table, and reading it as one produced the 1309A pin names under a
            # scope correctly narrowed to the 1308A table.
            if len(groups) < 2:
                continue
            idx = next((i for i, g in enumerate(groups)
                        if any(t is pick[1] or t == pick[1] for _, t in g)), None)
            if idx is None:
                continue
            got = {}
            # Is this REALLY a multi-column table? A dash marks "absent in this
            # package" and is not a column of numbers; counting it as one turns
            # a plain NAME/NO./TYPE table into a fake two-column one. TPS1H200A
            # is exactly that, and the fake reading produced four partial pins
            # AND gated row_per_pin, which reads the table correctly.
            #
            # Judged over the whole table, not per row: individual rows legally
            # carry a dash in one package's column.
            digit_cols = set()
            for ln in lines:
                cells = [(m.start(), m.group(0)) for m in re.finditer(r"\S+", ln)]
                if len(cells) < 3 or not _NAME_CELL.fullmatch(cells[0][1]):
                    continue
                body = [c for c in cells[1:] if _NUM_CELL.fullmatch(c[1])]
                for j, c in enumerate(body):
                    if c[1].isdigit():
                        digit_cols.add(j)
            if len(digit_cols) < 2:
                continue

            sums = set()
            for ln in lines:
                # "6, 9" is one cell: TXU0204 lists both NC pins in one row.
                cells = [(m.start(), m.group(0)) for m in
                         re.finditer(r"\d{1,3}(?:\s*,\s*\d{1,3})+|\S+", ln)]
                if len(cells) < 3:
                    continue
                name = cells[0][1]
                if not _NAME_CELL.fullmatch(name) or name.upper() in ("NAME", "PIN"):
                    continue
                body = [c for c in cells[1:] if _NUM_CELL.fullmatch(c[1])
                        or _NUM_LIST_CELL.fullmatch(c[1])]
                if len(body) <= idx or len(body) < len(groups):
                    continue
                val = body[idx][1]
                if val in ("-", "—") or not val[0].isdigit():
                    continue
                if body[0][1].isdigit() and body[1][1].isdigit():
                    sums.add(int(body[0][1]) + int(body[1][1]))
                for p in E.expand_nums(val):
                    got[p] = name
            # Every row summing to the same N+1 is a pinout DRAWING ("D  3  6
            # S"), which numbers down one side and up the other. Read as a
            # table it gives pin 6 the name of pin 3.
            if len(got) >= 3 and len(sums) == 1:
                continue
            if len(got) > len(pins):
                pins, why = got, (f"column {pick[1]} (position {idx + 1} of "
                                  f"{len(groups)})")
    return Result("name_then_columns", pins, note=why)


_MINIMAL_NAME = re.compile(r"[A-Z][A-Z0-9_+/\-]{1,15}")


def shape_number_name_only(text, ctx):
    """Rows of NUMBER + NAME, with or without a trailing prose description.

        PIN NUMBER   SYMBOL      DESCRIPTION        Pin  Symbol  Description
             1        VCIN                            1    E1    emitter TR1
             2        PGOOD                           2    B1    base TR1

    Vishay's SiC473 puts the description on separate lines; Nexperia's BC857BS
    puts it on the same one. Neither has a type column, so every typed-row
    pattern sees nothing -- which is why the dual transistors read as zero.

    The form is trivially easy to match by accident ("1 Introduction to the
    device" has the same shape), so the guards are the substance: the number
    must fall inside the part's pin range, the NAME cell must look like a pin
    name and not a capitalised English word, anything after it must be prose
    rather than more table, and at least three such rows must appear together.
    """
    limit = ctx.total_pins or 300
    best = {}
    for page in text.split("\f"):
        found, under_pin_heading = {}, False
        for ln in page.split("\n"):
            cells = [m.group(0) for m in re.finditer(r"\S+", ln)]
            if len(cells) < 2:
                continue
            # Rows count only under a heading that numbers PINS. A register
            # table numbers bits, and its field names have exactly the shape of
            # pin names: S25FL064L's status and protection registers read as
            # pins 1-7 (WEL_D, BP_NV0, ...) of a SOIC-8.
            heads = _cells(ln)
            if all(all(_HEAD_WORD.fullmatch(w) or re.fullmatch(r"\(\d\)", w)
                       for w in t.split()) for _, t in heads):
                kinds = {("num" if _HEAD_NUM.fullmatch(t) else "") for _, t in heads}
                if re.fullmatch(r"Bits?", cells[0], re.I):
                    found, under_pin_heading = {}, False
                elif "num" in kinds or re.match(r"PIN\b", ln.strip(), re.I):
                    under_pin_heading = True
                continue
            if not under_pin_heading:
                continue
            num, name = cells[0], cells[1]
            if num == "0" and _MINIMAL_NAME.fullmatch(name):
                found = {}                   # bit 0: no package has a pin 0
                continue
            if not num.isdigit() or not 1 <= int(num) <= limit:
                continue
            if not _MINIMAL_NAME.fullmatch(name):
                continue
            # A capitalised word is prose, not a pin: "Introduction", "Features".
            if name.isalpha() and name[1:].islower():
                continue
            # Whatever follows must be description, not further table columns.
            # If the third cell is also pin-name-shaped this is a wider table
            # and belongs to a column shape, which reads it more precisely.
            if len(cells) > 2 and _MINIMAL_NAME.fullmatch(cells[2]) \
                    and not cells[2].isalpha():
                continue
            # A PARAMETER row, not a pin row. Electrical-characteristics tables
            # are full of "TA = +25°C", "PW = 10s", "ID = -5A", and with a
            # column of test-condition numbers to their left they have exactly
            # the shape of a pin row. Diodes' DMP6023 has no pin table at all --
            # it shows a package drawing -- and this produced three confident
            # inventions from its ratings tables: pin 2 "TA", pin 4 "PW", pin 6
            # "DC". The assignment or comparison right after the symbol is the
            # giveaway, and it is a convention every vendor follows.
            if len(cells) > 2 and cells[2] in ("=", "≤", "≥", "<", ">", "±",
                                               "@", "~"):
                continue
            found.setdefault(num, name)
        if len(found) >= 3 and len(found) > len(best):
            best = found
    return Result("number_name_only", best)


# Heading cells of a pin table, classified by what the column holds. Anything
# else in a heading (TYPE, I/O, DESCRIPTION, "Simplified outline") is simply
# another column; it is only there to bound the two that matter.
_HEAD_NUM = re.compile(r"(?:(?:PIN|BALL|PAD|TERMINAL)\s*)?(?:#|NO\.?|NUMBER|NUM\.?)"
                       r"|PIN|BALL|TERMINAL|PAD", re.I)
_HEAD_NAME = re.compile(r"(?:(?:PIN|SIGNAL|BALL)\s+)?NAME|SYMBOL|SIGNAL|MNEMONIC",
                        re.I)
_HEAD_WORD = re.compile(r"[A-Za-z#][A-Za-z#./\-]*(?:\s?\(\d\))?")
# A numbered section heading ("6 Specifications", "7.1 Absolute Maximum"), a
# caption or a footnote: each ends the table above it.
_TABLE_END = re.compile(r"^\s{0,4}(?:\d+(?:\.\d+)*\.?\s(?=\S)[A-Z][A-Za-z0-9 ]*[a-z]"
                        r"|(?:Table|Figure)\s+[\d\-.]+|\(\d\)\s)")
_NUM_LIST = re.compile(r"\d{1,3}(?:\s*[,–—-]\s*\d{1,3})*|[—–-]")
_PIN_NAME = re.compile(r"[A-Za-z~][A-Za-z0-9_+\-–/.#~()\[\]]{0,23}")
# The unnumbered exposed pad, as a table names it ("Thermal pad", "PAD").
_PAD_NAME = re.compile(r"(?:thermal|exposed|power)\s*pad|powerpad|e?pad|ep",
                       re.I)


def _cells(line):
    """-> [(x, text)], splitting on runs of two or more spaces."""
    return [(m.start(), m.group(0)) for m in re.finditer(r"\S+(?: \S+)*", line)]


def _heading(line):
    """-> [(x, kind, text)] if the line is a pin table's heading, else None.

    kind is "num", "name" or "other". A heading holds exactly one number column
    and one name column; a line with several number columns belongs to a
    multi-package table, which the column shapes read."""
    cells = _cells(line)
    if not 2 <= len(cells) <= 8:
        return None
    out = []
    for x, t in cells:
        words = t.split()
        if len(words) > 3 or not all(_HEAD_WORD.fullmatch(w) or re.fullmatch(
                r"\(\d\)", w) for w in words):
            return None
        core = re.sub(r"\s*\(\d\)$", "", t)
        kind = ("num" if _HEAD_NUM.fullmatch(core) else
                "name" if _HEAD_NAME.fullmatch(core) else "other")
        out.append((x, kind, t))
    kinds = [k for _, k, _ in out]
    if kinds.count("num") != 1 or kinds.count("name") != 1:
        return None
    return out


def _heading_at(lines, i):
    """_heading() of line i, or of a heading whose number column is stacked
    over three lines, as Quectel sets it:

                       Pin
         Pin Name               I/O    Description
                       No.
    """
    head = _heading(lines[i])
    if head or not 0 < i < len(lines) - 1:
        return head
    above, below = _cells(lines[i - 1]), _cells(lines[i + 1])
    if len(above) != 1 or len(below) != 1:
        return None
    (xa, ta), (xb, tb) = above[0], below[0]
    if abs(xa - xb) > 4 or not _HEAD_NUM.fullmatch(f"{ta} {tb}"):
        return None
    mid = lines[i].ljust(xb + len(tb) + 2)
    if mid[max(0, xb - 2):xb + len(tb) + 2].strip():
        return None
    return _heading(mid[:xb] + tb + mid[xb + len(tb):])


def _heading_block(lines, i, head):
    """The heading line plus the lines above it that are also heading text.

    TI splits a heading over two lines -- "PIN" and "TYPE  DESCRIPTION" above
    "NAME  NO." -- so the TYPE column is only known from the line above, and
    without it a type cell is taken for the name."""
    cols = list(head)
    for j in (i - 1, i - 2):
        if j < 0 or not lines[j].strip():
            break
        cells = _cells(lines[j])
        if not all(all(_HEAD_WORD.fullmatch(w) or re.fullmatch(r"\(\d\)", w)
                       for w in t.split()) for _, t in cells):
            break
        for x, t in cells:
            # A title above the heading ("PIN DESCRIPTION") spans the columns
            # below it rather than naming a new one.
            if any(x < hx + len(ht) and hx < x + len(t)
                   for hx, k, ht in head if k != "other"):
                continue
            cols.append((x, "other", t))
    return sorted(cols)


def _column_of(x, text, cols):
    """The heading column nearest to a cell, by start or by centre, whichever
    is closer: vendors left-align some columns and centre others, and a long
    pin list ("3, 31, 48, 50, ...") is only near its heading at its start."""
    mid = x + len(text) / 2.0
    return min(cols, key=lambda c: min(abs(c[0] - x),
                                       abs(c[0] + len(c[2]) / 2.0 - mid)))[1]


def _tidy_name(name):
    # A trailing "*" or "(1)" marks a footnote, not part of the name.
    name = re.sub(r"(?:\*+|\s*\(\d\))$", "", name.strip())
    name = re.sub(r"\s*/\s*", "/", name)
    return name.replace("–", "-").replace("—", "-")


def _read_under_heading(lines, i, cols):
    """-> ({pin: name}, pad_name) for the rows below the heading at line i.

    A long pin list wraps, and a cell is centred on its row, so the list can
    start on the line ABOVE its name and end below it:

                  11-14, 16, 56, 57, 63-66, 78, 88, 92-95, 97-99, 103-118,
        RESERVED                                                   Keep them open.
                  123-125

    A list ending in a comma is open: the next list alone in the number column
    continues it, and a list with no name waits for the name on the next line.
    """
    rows, pending, pad = [], None, None      # rows: [name, nums, open]
    idle = 0
    for j in range(i + 1, len(lines)):
        ln = lines[j]
        if _TABLE_END.match(ln) or _heading_at(lines, j):
            break
        # A table runs on over page breaks, but not through unrelated pages.
        idle += 1
        if idle > 60:
            break
        cells = _cells(ln)[:2]
        kinds = [_column_of(x, t, cols) for x, t in cells]
        num = next((t for (x, t), k in zip(cells, kinds) if k == "num"), None)
        name = next((t for (x, t), k in zip(cells, kinds) if k == "name"), None)
        if num is not None:
            open_ = num.endswith(",")
            num = num.rstrip(",").strip()
            if not _NUM_LIST.fullmatch(num):
                num = None
        if num is not None and name is None:
            if rows and rows[-1][2]:
                rows[-1][1] += ", " + num
                rows[-1][2] = open_
            elif open_:
                pending = num
            continue
        if name is None:
            continue
        if num is None:
            if pending is None:
                continue
            num, open_, pending = pending, True, None
        pending = None
        name = _tidy_name(name)
        if num in ("—", "–", "-"):
            # No number: only the exposed pad is listed like that.
            if _PAD_NAME.fullmatch(name.replace(" ", "")) or \
                    re.fullmatch(r"(?:thermal|exposed)\s+pad", name, re.I):
                pad = name
            continue
        if not _PIN_NAME.fullmatch(name) or name.upper() in E._NOISE:
            continue
        rows.append([name, num, open_])
        idle = 0
    pins = {}
    for name, num, _ in rows:
        for p in E.expand_nums(num):
            pins.setdefault(p, name)
    return pins, pad


def shape_header_table(text, ctx):
    """A pin table found by its HEADING, read column by column.

        Pin #   Pin Name   Type     Pin Description        PIN NUMBER   SYMBOL
          1       VDD      PWR      VDD supplies ...          5, 6       PHASE
        3, 4       D       MOSFET   Drain terminal ...

    The typed-row patterns need a type cell they recognise, and every vendor
    has one they do not ("MOSFET", "Analog input"). The heading names the
    number and name columns outright, so a row is read by WHERE its cells sit,
    not by what the type cell happens to say.

    Several tables in one document are several candidates: one per package or
    per device. They are kept apart, those that name pins the part cannot have
    are dropped, and if the rest disagree nothing is returned -- the section
    scoping upstream is what should have picked one.
    """
    # One list of lines for the whole document: rows at the top of a page,
    # before its first heading, belong to the table the last page left open.
    lines, page_of = [], []
    for page_no, page in enumerate(text.split("\f"), 1):
        for ln in page.split("\n"):
            lines.append(ln)
            page_of.append(page_no)
    tables = []
    for i in range(len(lines)):
        head = _heading_at(lines, i)
        if not head:
            continue
        pins, pad = _read_under_heading(lines, i, _heading_block(lines, i, head))
        if pins:
            sig = tuple(re.sub(r"\W", "", t).upper() for _, _, t in head)
            tables.append((page_of[i], pins, pad, sig))
    if not tables:
        return Result("header_table", {})

    # Pieces under the same heading that never contradict each other are one
    # table: continued over a page, or cut into sections that each repeat the
    # heading (Quectel's "Power Supply", "USB Interface", ...). Two tables
    # that name one pin differently -- two devices, two packages -- stay apart.
    # A piece right after the last one, under a different heading, joins it
    # too: Quectel closes the table with "Pin Name  Pin No.  Comment" for the
    # RESERVED row.
    merged = []
    for page_no, pins, pad, sig in tables:
        for k, (pg, prev, prev_pad, psig) in enumerate(merged):
            if (psig == sig or page_no - pg <= 1) and not any(
                    _differs(prev[p], pins[p]) for p in set(prev) & set(pins)):
                merged[k] = (page_no, {**prev, **pins}, prev_pad or pad, sig)
                break
        else:
            merged.append((page_no, pins, pad, sig))

    limit = ctx.total_pins
    fits = [t for t in merged if not limit or all(
        int(p) <= limit for p in t[1] if p.isdigit())]
    if not fits:
        return Result("header_table", {}, note=f"{len(merged)} table(s) under "
                      f"a pin heading, none fits a {limit}-pin part")
    best = max(fits, key=lambda t: len(t[1]))
    rivals = [t for t in fits if t is not best and any(
        _differs(t[1][p], best[1][p]) for p in set(t[1]) & set(best[1]))]
    if rivals:
        return Result("header_table", {}, note=(
            f"{len(fits)} tables under a pin heading disagree (pages "
            f"{', '.join(str(t[0]) for t in fits)}); not choosing"))
    pins = dict(best[1])
    if best[2]:
        pins["EP"] = best[2]
    return Result("header_table", pins, note=f"page {best[0]}")


_DIAGRAM_NAME = r"[A-Za-z~][A-Za-z0-9_+\-–/.#~]*(?: / [A-Za-z0-9_+\-#]+)?"
_DIAGRAM_ROW = re.compile(
    rf"(?:(?<=\s\s)|^)\s*(?P<l>{_DIAGRAM_NAME})\s+(?P<n>\d{{1,3}})\s{{2,}}"
    rf"(?P<m>\d{{1,3}})\s+(?P<r>{_DIAGRAM_NAME})(?=\s{{2,}}|\s*$)")


def shape_pinout_diagram(text, ctx):
    """A dual-row pinout drawing, as pdftotext lays it out.

            CS#      1              8     VCC
         SO / IO1    2              7     IO3 / RESET#
                             SOIC 8
        WP# / IO2    3              6     SCK
              VSS    4              5     SI / IO0

    Some datasheets have no pin table at all -- S25FL064L only has these
    drawings -- and every dual-row package numbers down one side and back up
    the other. That is the check: each row's two numbers sum to N+1, and the
    left side runs 1..N/2 without a gap. A text fragment that happens to hold
    two numbers between two words fails it.

    A document draws one per package. Drawings for another pin count are
    dropped, identical drawings (SOIC-8, USON-8 and WSON-8 share a pinout) are
    one answer, and different ones need the package named beside them.
    """
    found = []
    for page_no, page in enumerate(text.split("\f"), 1):
        lines = page.split("\n")
        for i, ln in enumerate(lines):
            for m in _DIAGRAM_ROW.finditer(ln):
                found.append((page_no, i, m.start("n"), int(m.group("n")),
                              int(m.group("m")), _tidy_name(m.group("l")),
                              _tidy_name(m.group("r"))))

    # Rows of one drawing share a page, a column for the left numbers and a
    # constant sum; side-by-side drawings on one line stay apart by column.
    drawings = []
    for row in found:
        page_no, i, x, n, m = row[:5]
        for d in drawings:
            last = d[-1]
            if (last[0] == page_no and abs(last[2] - x) <= 3
                    and last[3] + last[4] == n + m and 0 < i - last[1] <= 12):
                d.append(row)
                break
        else:
            drawings.append([row])

    total = ctx.total_pins
    complete = []
    for d in drawings:
        s = d[0][3] + d[0][4]
        lefts = [r[3] for r in d]
        if s % 2 == 0 or sorted(lefts) != list(range(1, s // 2 + 1)):
            continue
        if total and s - 1 != total:
            continue
        pins = {}
        for r in d:
            pins[str(r[3])], pins[str(r[4])] = r[5], r[6]
        page = text.split("\f")[d[0][0] - 1].split("\n")
        near = " ".join(page[max(0, d[0][1] - 4):d[-1][1] + 6])
        complete.append((pins, near, d[0][0]))
    if not complete:
        return Result("pinout_diagram", {})

    distinct = []
    for pins, near, page_no in complete:
        if not any(pins == p for p, _, _ in distinct):
            distinct.append((pins, near, page_no))
    if len(distinct) == 1:
        pins, _, page_no = distinct[0]
        return Result("pinout_diagram", pins,
                      note=f"drawing on page {page_no}; confirm pin 1")

    hint = re.sub(r"[^A-Z0-9]", "", ctx.package_hint.upper())
    named = [(p, pg) for p, near, pg in distinct
             if any(len(k) >= 4 and k in hint for k in
                    (re.sub(r"[^A-Z0-9]", "", w.upper())
                     for w in re.findall(r"[A-Z][A-Za-z]+[- ]?\d{0,3}", near)))]
    if len(named) == 1:
        return Result("pinout_diagram", named[0][0],
                      note=f"drawing on page {named[0][1]}, named for the "
                           f"package; confirm pin 1")
    counts = "/".join(sorted({str(len(p)) for p, _, _ in distinct}))
    return Result("pinout_diagram", {}, note=(
        f"{len(distinct)} different {counts}-pin drawings and none named for "
        f"this package; not choosing"))


def shape_wrapped_names(text, ctx):
    """Repair pass: names split across lines by a vertically centred cell."""
    if not ctx.package_hint:
        return Result("wrapped_names", {})
    pins, low = {}, []
    for page in text.split("\f"):
        cols = E._columns_from_headers(page)
        if len(cols) < 2:
            continue
        x, _ = E.choose_column(page, ctx.family or ctx.mpn[:8], ctx.package_hint)
        if x is None:
            continue
        got = E.recover_wrapped_names(page, len(cols), x)
        for pin, name in got.items():
            pins.setdefault(pin, name)
            low.append(pin)
    return Result("wrapped_names", pins, low_confidence=low,
                  note="reassembled from split cells; verify against the PDF")


# Order matters: the most structured shapes first, so a weaker shape can only
# ADD pins the stronger ones did not find, never overwrite them.
SHAPES = (shape_number_columns, shape_header_table, shape_pinout_diagram,
          shape_name_then_columns,
          shape_row_per_pin,
          shape_number_name_only, shape_name_with_pin_list, shape_wrapped_names,
          shape_prose_ranges)


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------

def read_pin_table(text, ctx, shapes=SHAPES):
    """Read one part's pin table from a whole datasheet. -> dict.

    Fixed pipeline, same for every document, so behaviour is predictable rather
    than tuned per PDF:

      1. SCOPE   -- if the document publishes one table per package or per part,
                    narrow to the one matching this MPN/footprint. Reading them
                    all merged is how a 5-pin part acquires nine pins.
      2. APPLY   -- run every shape that yields anything, strongest first. A
                    later shape may only ADD pins, never overwrite; where two
                    shapes disagree on a name, both are recorded.
      3. VALIDATE-- refuse a table with more pins than the part has, and report
                    what is missing rather than implying completeness.
    """
    out = {
        "pins": {}, "by_shape": {}, "conflicts": {}, "low_confidence": set(),
        "scope": None, "notes": [], "overextracted": 0,
    }

    # Drop contents/index pages up front: they are dense with section titles
    # that read like table headings, and they come first, so every shape that
    # scans front-to-back meets them before the real table.
    pages = text.split("\f")
    kept = [p for p in pages if not E.is_contents_page(p)]
    if kept and len(kept) < len(pages):
        out["notes"].append(f"skipped {len(pages) - len(kept)} contents/index "
                            f"page(s)")
        text = "\f".join(kept)

    scoped = E.choose_section(text, [ctx.package_hint, ctx.mpn])
    if scoped:
        out["scope"] = scoped[0]
        text = scoped[1]

    # Run every applicable shape, then merge BEST-COVERAGE FIRST rather than in
    # declaration order. Order alone is the wrong tie-breaker: a shape that
    # matched some unrelated two-column table wins simply by being declared
    # earlier, and on ADS114S08 that put REFP1/AIN6 on pin 9 where the real
    # table says CS.
    #
    # Coverage is measured against the pin count the ORDER CODE or package size
    # implies -- never the symbol -- so this ranks readings of the DOCUMENT
    # without consulting the thing under test.
    results = []
    for shape in shapes:
        try:
            r = shape(text, ctx)
        except Exception as e:                                   # noqa: BLE001
            out["notes"].append(f"{shape.__name__}: raised {type(e).__name__}")
            continue
        if not r.pins:
            continue
        if len(r.pins) < 2:
            out["notes"].append(f"{r.shape}: ignored, found only "
                                f"{len(r.pins)} pin (too few to be a table)")
            continue
        results.append(r)

    total = ctx.total_pins

    def coverage(r):
        if not total:
            return len(r.pins) / 1000.0
        inrange = sum(1 for p in r.pins
                      if str(p).isdigit() and 1 <= int(p) <= total)
        return inrange / float(total)

    results.sort(key=coverage, reverse=True)
    if results:
        out["notes"].append("shape ranking by coverage: " + ", ".join(
            f"{r.shape}={coverage(r):.0%}" for r in results))

    structured, too_full = False, 0
    for r in results:
        if structured and r.shape in _SCAVENGERS:
            continue
        # A reading that names a pin the package does not have has read some
        # other table, a footer or a dimension, and its other rows are no more
        # trustworthy than that one. The whole reading goes, not just the pin.
        beyond = sorted((p for p in r.pins if str(p).isdigit()
                         and total and int(p) > total), key=int)
        if beyond:
            out["notes"].append(
                f"{r.shape}: discarded, it names pin(s) "
                f"{', '.join(E.compress(beyond))} of a {total}-pin package")
            if len(r.pins) > total:
                too_full = max(too_full, len(r.pins))
            continue
        out["by_shape"][r.shape] = len(r.pins)
        if r.note:
            out["notes"].append(f"{r.shape}: {r.note}")
        for pin, name in r.pins.items():
            if pin == "EP":
                # The unnumbered exposed pad takes the number KiCad gives it,
                # which needs both the lead count and exactly one such pad.
                if not (ctx.expect_pins and ctx.exposed_pads == 1):
                    out["notes"].append(f"{r.shape}: {name!r} has no number and "
                                        f"the footprint has no single exposed pad")
                    continue
                pin = str(ctx.expect_pins + 1)
                out["notes"].append(f"{r.shape}: {name!r} is the exposed pad, "
                                    f"numbered {pin} as the footprint does")
                out["low_confidence"].add(pin)
            if ctx.numeric_pins and not str(pin).isdigit():
                out.setdefault("rejected_ids", []).append(f"{r.shape}:{pin}")
                continue
            if pin not in out["pins"]:
                out["pins"][pin] = name
                if pin in r.low_confidence:
                    out["low_confidence"].add(pin)
            elif _differs(out["pins"][pin], name):
                out["conflicts"].setdefault(pin, []).append((r.shape, name))

        # A column table that produced NEARLY ALL of the part's pins is the
        # answer; anything else found later would be scavenged from elsewhere.
        if r.shape in _STRUCTURED and total and len(r.pins) >= max(
                2, 0.8 * total):
            structured = True
            out["notes"].append(
                f"{r.shape} read the table; generic row scanning skipped")

    if out.get("rejected_ids"):
        out["notes"].append(
            f"{len(out['rejected_ids'])} non-numeric pin id(s) rejected for a "
            f"numbered package (e.g. {', '.join(out['rejected_ids'][:4])})")

    n = len(out["pins"])
    if total and (n > total or (too_full and not n)):
        # No reading of this is usable, and a too-full table is more dangerous
        # than an empty one because it looks finished.
        out["overextracted"] = max(n, too_full)
        out["pins"] = {}
        out["notes"].append(
            f"refused: {out['overextracted']} pins found for a {total}-pin part "
            f"(tables merged, or a neighbouring table was read)")

    found = set(out["pins"])
    missing = ([str(i) for i in range(1, total + 1)
                if str(i) not in found] if total else [])
    out["missing"] = missing
    out["missing_ranges"] = E.compress(missing)
    out["complete"] = bool(total) and not missing
    return out


def _differs(a, b):
    norm = lambda s: re.sub(r"[^A-Z0-9]", "", str(s).upper())
    x, y = norm(a), norm(b)
    return bool(x) and bool(y) and x != y and x not in y and y not in x
