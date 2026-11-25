#!/usr/bin/env python3
"""Propose a pin table from datasheet text, and be honest about the gaps.

THE TEDIOUS TASK THIS EXISTS FOR. Transcribing a 100+ pin table by hand is the
single most boring step in validating a BOM, and boring steps get skipped. But a
partial transcription that LOOKS complete is worse than none, so the design rule
here is: extract what can be extracted, and make everything missing impossible to
miss.

WHAT REAL TABLES LOOK LIKE (measured on the PDFs in this repo, not assumed):

  TI, SCDS477A -- name-major, one pin per row:
      S0B                 1                 I/O           Source pin 0 of mux B.

  Quectel, EC21 hardware design -- name-major, one row covering SEVERAL pins:
      VBAT_BB           59, 60     PI      Power supply for module's BB part

  ...and, in the same document, whole ranges that appear ONLY in prose:
      "GND pins 85-112 should be connected to ground. RESERVED pins 73-84
       should not be designed in schematic"

That last one is ~40 of 146 pins that no table parser will ever see. Any tool
that reports a pin table for such a part without saying what it missed is lying.

STRATEGY. Several narrow recognisers, each reporting what it found and where,
merged into one table with per-pin provenance. Then a coverage report against the
part's declared pin count. Confidence is never inferred from how much was found
-- it comes from whether the coverage is complete and unanimous.
"""
import re

# A pin NAME: uppercase-ish, may carry digits, underscores, slashes, +/-.
_NAME = r"[A-Z][A-Z0-9_+\-/.]{0,23}"
# A pin NUMBER cell: "7", "59, 60", "85-112", "A1" (BGA), or combinations.
_NUMS = r"[0-9A-Z]{1,4}(?:\s*[,–—-]\s*[0-9A-Z]{1,4})*"
# The type column between the number and the description. Vendors write it
# either as an abbreviation (TI's TMUX: "I/O", Quectel: "PI") or as a full
# phrase (TI's ADS114S08B: "Analog input", "Digital ground"). Both must be
# recognised -- with only the abbreviations, the ADS table yields ZERO rows,
# which is how this was found.
_TYPE = (r"(?:I/O|IO|I|O|P|PI|PO|AI|AO|AIO|DI|DO|DIO|OD|NC|N\.C\.|GND|PWR|"
         r"(?:Analog|Digital|Power|Ground|Supply|Reference|Not)\s+[A-Za-z/]+|"
         # Bare single-word types. Renesas writes "PWR" / "Input" / "Output" in
         # the type column; without these the SLG59M301V table yields nothing.
         r"Input|Output|Inputs?/Outputs?|Bidirectional|Passive|Supply|Ground|"
         r"Power|Not\s+Connected|-|—)")

_ROW_NAME_FIRST = re.compile(
    rf"^\s*(?P<name>{_NAME})\s{{2,}}(?P<nums>{_NUMS})\s{{2,}}(?P<type>{_TYPE})(?=\s|$)",
    re.M)
_ROW_NUM_FIRST = re.compile(
    rf"^\s*(?P<nums>{_NUMS})\s{{2,}}(?P<name>{_NAME})\s{{2,}}(?P<type>{_TYPE})(?=\s|$)",
    re.M)

# One name owning a LIST of pins, with no type column at all:
#     RESERVED        18, 43, 55, 73-84, 113, 114, 116, 117, 140-144
# Real, and load-bearing: that single line on page 36 of the EC21 document
# accounts for 20+ pins the typed-row patterns cannot see. Kept safe by demanding
# the list actually be a list (a separator is required) and that the line END
# there -- otherwise this would match any prose sentence beginning with a word.
_ROW_NAME_NUMLIST = re.compile(
    rf"^\s*(?P<name>{_NAME})\s{{2,}}"
    rf"(?P<nums>[0-9]{{1,3}}(?:\s*[,–—-]\s*[0-9]{{1,3}})+)\s*$", re.M)

# "GND pins 85-112", "pins 73-84 should not be designed", "Pin 7: N.C."
_PROSE = re.compile(
    rf"(?:(?P<name1>{_NAME})\s+)?pins?\s+(?P<nums>\d{{1,3}}\s*[–—-]\s*\d{{1,3}}"
    rf"(?:\s*,\s*\d{{1,3}}(?:\s*[–—-]\s*\d{{1,3}})?)*)"
    rf"(?:\s+(?:should|are|is|must)\s+(?:be\s+)?(?P<name2>[A-Za-z ]{{0,24}}))?",
    re.I)

# Words that look like pin names but are table furniture.
# Careful: this list must contain ONLY table furniture. Real pin names look
# exactly like headers -- VDD, GND, EN and NC are all legitimate names, and
# listing one here silently deletes that pin from every table (VDD was here
# once; it cost pin 16 of the TMUX table).
_NOISE = {"PIN", "NAME", "NO", "NUMBER", "TYPE", "DESCRIPTION", "COMMENT",
          "TABLE", "FIGURE", "NOTE", "NOTES", "PARAMETER", "SYMBOL", "MIN",
          "MAX", "TYP", "UNIT", "CONDITIONS", "PAGE", "CONTENTS"}


def _valid_pin(tok):
    """A pin identifier a package can actually have.

    Pin numbering starts at 1 -- there is no pin 0 in any package -- and a
    Wuerth LED datasheet yielded {'0': 'R0', '8': 'R0'} from a reflow-profile
    table, which then promoted as a reference because it disagreed with the
    symbol structurally rather than by name."""
    t = str(tok).strip()
    if t.isdigit():
        return 1 <= int(t) <= 600
    return bool(re.fullmatch(r"[A-Z]{1,2}[0-9]{1,2}", t))


def expand_nums(cell):
    """'59, 60' -> ['59','60']; '85-112' -> ['85'..'112']; 'A1' -> ['A1'].

    Ranges are expanded ONLY when both ends are numeric and ascending and the
    span is sane. A silently mis-expanded range would invent pin names, which is
    the one failure mode worse than missing pins."""
    out = []
    parts = [p.strip() for p in re.split(r"\s*,\s*", cell.strip())]
    # "0,8" is a decimal comma -- a Wuerth LED's 0.8 mm dimension read as pins
    # 0 and 8 -- and no pin list starts a number with a zero.
    if any(re.match(r"0\d*$", p) for p in parts):
        return []
    for part in parts:
        if not part:
            continue
        m = re.match(r"^(\d+)\s*[–—-]\s*(\d+)$", part)
        if m:
            # Range-SHAPED: either it expands sanely or it is dropped. Never
            # emitted verbatim -- a pin numbered "1-9999" is not a pin, and
            # inventing pin numbers is the one failure worse than missing them.
            a, b = int(m.group(1)), int(m.group(2))
            if a < b and (b - a) <= 400 and b <= 2000:
                out.extend(str(i) for i in range(a, b + 1))
            continue
        out.append(part)
    return [p for p in out if _valid_pin(p)]


def _clean_name(n):
    n = n.strip()
    # "N.C." keeps its last period: it is an abbreviation, not punctuation.
    if not re.fullmatch(r"(?:[A-Z]\.){2,}", n):
        n = n.rstrip(".,;:")
    return n


def _plausible(name, nums):
    if not nums:
        return False
    n = _clean_name(name).upper()
    if n in _NOISE or len(n) < 1:
        return False
    # A "name" that is itself a number is a mis-parse of a two-number column.
    if n.isdigit():
        return False
    return True


def recognise_rows(text, page_no=0):
    """-> [(pin, name, how, page)] from tabular rows on one page."""
    found = []
    for how, rx in (("row:name-first", _ROW_NAME_FIRST),
                    ("row:number-first", _ROW_NUM_FIRST),
                    ("row:name+numlist", _ROW_NAME_NUMLIST)):
        for m in rx.finditer(text):
            name, nums = _clean_name(m.group("name")), m.group("nums")
            if not _plausible(name, nums):
                continue
            for pin in expand_nums(nums):
                found.append((pin, name, how, page_no))
    return found


def recognise_prose(text, page_no=0):
    """-> [(pin, name, how, page)] from prose that names whole ranges.

    Only fires on an explicit range, because prose is where confident nonsense
    comes from. A range with no adjacent name yields the name RESERVED?/None so
    the human sees the pins exist but must label them."""
    found = []
    for m in _PROSE.finditer(text):
        nums = m.group("nums")
        pins = expand_nums(nums)
        if len(pins) < 2:
            continue
        name = _clean_name(m.group("name1") or "")
        tail = (m.group("name2") or "").strip().lower()
        if not name:
            if "reserved" in tail:
                name = "RESERVED"
            elif "ground" in tail or "gnd" in tail:
                name = "GND"
            else:
                continue                    # a range with no name is not evidence
        if name.upper() in _NOISE:
            continue
        for pin in pins:
            found.append((pin, name.upper(), "prose:range", page_no))
    return found


_SECTION_HEAD = re.compile(
    # An optional "Table 5-2." prefix: TI heads these as
    # "Table 5-2. Pin Functions TMUX1309A".
    r"^[ \t]*(?:Table\s+[\d\-.]+\.?[ \t]*)?"
    r"Pin\s+(?:Functions|Descriptions?|Assignments?|Configuration)"
    r"[ \t]*[:\u2014\-]?[ \t]*(?P<label>[A-Za-z0-9][A-Za-z0-9 ,/()\-]{0,40})?[ \t]*$",
    re.M | re.I)


def section_spans(text):
    """-> [(label, start, end)] for package- or part-scoped pin tables.

    A datasheet covering several packages often publishes ONE TABLE PER
    PACKAGE rather than one table with several columns:

        Pin Functions: DSBGA
        Pin Functions: X2SON, SOT-23

    Merging them is not a small error -- TPS7A2033PDBVR is a 5-pin SOT-23 part
    and merging its two tables yielded NINE pins, a table that is complete,
    confident and wrong. The same idiom appears part-scoped ("Pin Functions
    TMUX1308A" / "TMUX1309A") in a document covering two devices.
    """
    hits = [(m.start(), (m.group("label") or "").strip())
            for m in _SECTION_HEAD.finditer(text)]
    out = []
    for i, (pos, label) in enumerate(hits):
        if i + 1 < len(hits):
            end = hits[i + 1][0]
        else:
            # The LAST section must not run to end-of-document, or it swallows
            # everything after it -- for the TMUX datasheet that means the
            # ordering addendum, whose rows look enough like pins to yield 21
            # of them for a 16-pin part. A scoped table is at most a page or
            # two, so bound it at the second page break.
            end = pos
            for _ in range(2):
                nxt = text.find("\f", end + 1)
                if nxt < 0:
                    end = len(text)
                    break
                end = nxt
        out.append((label, pos, end))
    return out


def choose_section(text, hints):
    """-> (label, sliced_text) for the scoped table matching any hint, else None.

    Hints are the package the MPN decodes to and the board footprint. Both are
    statements about which physical part is fitted, which is the only thing that
    can pick between two tables in the same document.

    The best-matching label wins, and a tie between two different tables picks
    neither. A device name is the strongest match: TI heads one table
    "TPD1E05U06-Q1 DPY" and the next "TPD4E05U06-Q1 DQA", and only the device
    name separates them from TPD4E05U06QDQARQ1.
    """
    spans = [s for s in section_spans(text) if s[0]]
    if len(spans) < 2:
        return None
    norm = lambda x: re.sub(r"[^A-Z0-9]", "", str(x).upper())
    keys = [norm(h) for h in hints if h]
    scored = []
    for label, a, b in spans:
        best = 0
        # A label can list several packages -- "X2SON, SOT-23" heads one table
        # covering both -- so each is matched separately. Comparing the whole
        # label as one string fails: "X2SONSOT23" neither contains nor is
        # contained by the board's "TSOT235".
        for part in re.split(r"[,/\s]+|\band\b", label):
            nl = norm(part)
            if len(nl) < 3:
                continue
            # "TPD4E05U06-Q1": the device name before a qualifier suffix.
            dev = norm(part.split("-")[0])
            for k in keys:
                if len(dev) >= 5 and dev[0].isalpha() and k.startswith(dev):
                    best = max(best, 3)
                elif nl in k or k in nl:
                    best = max(best, 2 if len(nl) >= 5 else 1)
        if best:
            scored.append((best, label, a, b))
    if not scored:
        return None
    top = max(s[0] for s in scored)
    winners = [s for s in scored if s[0] == top]
    if len({w[1] for w in winners}) > 1:
        return None
    _, label, a, b = winners[0]
    return label, text[a:b]


_DOTLEADER = re.compile(r"\.\s?\.\s?\.\s?\.")
_TRAILING_PAGENO = re.compile(r"\.{2,}\s*\d{1,3}\s*$")


def is_contents_page(page):
    """True for a table of contents, index, or revision-history page.

    These pages are dense with section titles that read exactly like table
    headings and pin names, and they appear BEFORE the real content, so anything
    scanning front-to-back finds them first. This has misled three different
    parts of this tool: the ordering-scheme reader returned an empty scheme from
    the contents page, section scoping matched "Pin Descriptions ....... 10",
    and the pin shapes scavenge rows out of index entries.

    Detected structurally, by dot leaders and trailing page numbers, so it works
    regardless of what the document calls the section.
    """
    lines = [l for l in page.split("\n") if l.strip()]
    if not lines:
        return False
    leaders = sum(1 for l in lines if _DOTLEADER.search(l))
    pagenos = sum(1 for l in lines if _TRAILING_PAGENO.search(l))
    return (leaders >= 4 and leaders >= 0.25 * len(lines)) or pagenos >= 5


def page_scores(pages):
    """-> [(page_index, score)] ranking pages by how much they look like a pin
    table. Used to SUGGEST a chapter to a human, never to decide silently."""
    out = []
    for i, p in enumerate(pages, 1):
        if is_contents_page(p):
            continue
        rows = recognise_rows(p, i)
        distinct = len({r[0] for r in rows})
        if distinct >= 4:
            out.append((i, distinct))
    out.sort(key=lambda x: -x[1])
    return out


def extract(text, pages_wanted=None, pin_count=0, use_prose=True):
    """-> dict(pins, conflicts, coverage, sources, pages_used)

    pins:      {pin_number: name}          merged, majority-free (see conflicts)
    conflicts: {pin: [(name, how, page)]}  more than one name proposed
    coverage:  dict with declared/found/missing
    """
    pages = text.split("\f")
    idxs = (pages_wanted if pages_wanted
            else [i for i, _ in page_scores(pages)])
    votes = {}
    for i in idxs:
        if not (1 <= i <= len(pages)):
            continue
        for pin, name, how, pg in recognise_rows(pages[i - 1], i):
            votes.setdefault(pin, []).append((name, how, pg))
    if use_prose:
        # Prose ranges are searched over the WHOLE document: the footnote that
        # says "GND pins 85-112" sits beside the pin DIAGRAM, chapters away from
        # the pin table it completes.
        for i, p in enumerate(pages, 1):
            for pin, name, how, pg in recognise_prose(p, i):
                votes.setdefault(pin, []).append((name, how, pg))

    pins, conflicts = {}, {}
    for pin, vs in votes.items():
        names = {v[0] for v in vs}
        if len(names) == 1:
            pins[pin] = vs[0][0]
        else:
            # Prefer a tabular row over prose; a real table beats a footnote.
            tab = [v for v in vs if v[1].startswith("row:")]
            tabnames = {v[0] for v in tab}
            if len(tabnames) == 1:
                pins[pin] = tab[0][0]
            else:
                conflicts[pin] = vs
                pins[pin] = sorted(names)[0]

    numeric = sorted((int(p) for p in pins if str(p).isdigit()))
    declared = int(pin_count or 0)
    missing = []
    if declared:
        have = {str(p) for p in pins}
        missing = [str(i) for i in range(1, declared + 1) if str(i) not in have]
    elif numeric:
        top = numeric[-1]
        have = {str(p) for p in pins}
        missing = [str(i) for i in range(1, top + 1) if str(i) not in have]

    # More pins than the part HAS means tables were merged, or a neighbouring
    # table was read. That is a wrong table wearing the appearance of a complete
    # one, so it is refused rather than reported.
    over = bool(declared) and len(pins) > declared
    return {
        "pins": {} if over else pins,
        "conflicts": conflicts,
        "pages_used": idxs,
        "overextracted": len(pins) if over else 0,
        "coverage": {"declared": declared, "found": 0 if over else len(pins),
                     "missing": [str(i) for i in range(1, declared + 1)] if over
                                else missing,
                     "missing_ranges": ([f"1-{declared}"] if over
                                        else compress(missing))},
    }


def compress(nums):
    """['73','74','75','80'] -> ['73-75','80'] so a gap report is readable."""
    ints = sorted(int(n) for n in nums if str(n).isdigit())
    others = sorted(n for n in nums if not str(n).isdigit())
    out, i = [], 0
    while i < len(ints):
        j = i
        while j + 1 < len(ints) and ints[j + 1] == ints[j] + 1:
            j += 1
        out.append(str(ints[i]) if i == j else f"{ints[i]}-{ints[j]}")
        i = j + 1
    return out + others


# ---------------------------------------------------------------------------
# Multi-package tables (ST style)
# ---------------------------------------------------------------------------
#
# ST publishes ONE pin table covering every package a die ships in, as a row of
# pin-number columns followed by the pin name:
#
#     23  L2  L2  34  34  J9  K3  K3  21  31  K3  J9  J9  K3   PA0   I/O  FT_a
#     |________________ 14 package columns _______________|   name  type
#
# and the column headers are printed ROTATED, so pdftotext emits them one per
# line. Worse, the same package appears TWICE -- once per device family -- with
# different numbering: in DS12023, LQFP100 is column 0 for STM32L4R5xxx/L4R7xxx
# and column 8 for STM32L4R9xxx.
#
# Getting that choice wrong is not a near miss. Measured against the board's own
# 100-pin symbol: column 0 agrees on 88 of 89 pins, column 8 agrees on 15 of 80.
# A wrong column yields a confident, complete, entirely wrong pin table.
#
# So the column MUST be chosen from the document, never by seeing which column
# best fits the symbol -- the symbol is the thing under test, and fitting the
# evidence to it would validate anything.

# Package names as they head a pin-number column. Two shapes: a family plus a
# pin count (LQFP100, UFBGA169) and a bare family name (SC70, SOT-23, TO-92,
# DSBGA, X2SON). Microchip's MCP9701 table is three bare-name columns, and
# requiring a digit suffix made it invisible.
_PKG_HEADER = re.compile(
    r"\b((?:LQFP|UFBGA|UBGA|WLCSP|TFBGA|LFBGA|VFQFPN|UFQFPN|QFN|BGA)"
    r"\d{2,3}(?:_SMPS)?"
    r"|(?:SC|SOT|TO|TSSOP|MSOP|SOIC|SSOP|VSSOP|DFN|SON|X2SON|DSBGA|TDFN|WSON|"
    r"UDFN|QFN|VQFN|HTSSOP)[-\s]?\d{1,3}[A-Z]?"
    r"|DSBGA|X2SON)\b""")
# One group header can name SEVERAL families: "STM32L4R5xxx, STM32L4R7xxx" is a
# single column group. Matching per token would give 3 labels for 2 groups and
# misalign every lookup, so a comma-separated run is captured whole.
_FAMILY_LABEL = re.compile(
    r"\b([A-Z]{2,}[0-9][A-Z0-9]*x{2,}(?:\s*,\s*[A-Z]{2,}[0-9][A-Z0-9]*x{2,})*)\b")


# One package's cell in a pin row: a number, a ball, or absent. ST marks a pin
# a package lacks as "-" or, in some columns, "NC"; PB11 was lost to that.
_PKG_CELL = re.compile(r"[0-9]{1,3}|[A-Z]{1,2}[0-9]{1,2}|-|—|NC")


def _columns_from_headers(page):
    """-> [(x, package)] for the pin-number columns, left to right."""
    out = []
    for ln in page.split("\n"):
        for m in _PKG_HEADER.finditer(ln):
            out.append((m.start(), m.group(1)))
    out.sort()
    # A rotated header can be emitted on several lines at the same offset;
    # keeping both would double-count the column and break the group split.
    dedup, seen_x = [], set()
    for x, pkg in out:
        if x in seen_x:
            continue
        seen_x.add(x)
        dedup.append((x, pkg))
    return dedup


def _split_groups(cols):
    """Split columns into device-family groups at the first REPEATED package.

    Each family group lists the same package set, so a repeat is exactly where
    the next group begins. This avoids having to position the centred family
    labels, which pdftotext gives only a start offset for."""
    groups, cur, seen = [], [], set()
    for x, pkg in cols:
        if pkg in seen:
            groups.append(cur)
            cur, seen = [], set()
        cur.append((x, pkg))
        seen.add(pkg)
    if cur:
        groups.append(cur)
    # A lone column is a stray mention in body text, not a device-family group.
    # Microchip's page repeats "SOT-23" in prose after the table, which split one
    # real 3-column group into a group plus noise and made the reader refuse.
    real = [g for g in groups if len(g) >= 2]
    return real if real else groups


def _family_labels(page):
    """-> [label] in left-to-right order, one per device-family group."""
    hits, seen = [], set()
    for ln in page.split("\n"):
        # The table CAPTION ("Table 15. STM32L4Rxxx pin definitions") looks
        # exactly like a group header; it is not one.
        if re.search(r"\bTable\s+\d+|pin definitions", ln, re.I):
            continue
        for m in _FAMILY_LABEL.finditer(ln):
            tok = re.sub(r"\s+", " ", m.group(1)).strip()
            if len(tok) >= 8 and tok not in seen:
                seen.add(tok)
                hits.append((m.start(), tok))
    hits.sort()
    return [h[1] for h in hits]


def choose_column(page, family, package):
    """-> (x, why) for the pin-number column matching this part, or (None, why).

    `family` is the part's family stem (STM32L4R5 from STM32L4R5VIT6);
    `package` is normalised from the board footprint (LQFP100 from
    Package_QFP:LQFP-100_14x14mm_P0.5mm).
    """
    cols = _columns_from_headers(page)
    if not cols:
        return None, "no package column headers found on this page"
    groups = _split_groups(cols)
    labels = _family_labels(page)

    # The hint may be a bare package ("LQFP100") or a whole KiCad footprint
    # ("Package_TO_SOT_SMD:SOT-353_SC-70-5"), so matching is containment either
    # way -- SC70 is inside SOT353SC705, while SOT23 correctly is not.
    want_pkg = re.sub(r"[^A-Z0-9]", "", package.upper())
    want_fam = re.sub(r"[^A-Z0-9]", "", family.upper())

    def hit(pkg):
        n = re.sub(r"[^A-Z0-9]", "", pkg.upper())
        return bool(n) and (n == want_pkg or n in want_pkg or want_pkg in n)

    # Single family on the page: no ambiguity to resolve.
    if len(groups) == 1:
        for x, pkg in groups[0]:
            if hit(pkg):
                return x, f"only device family on the page; column {pkg} at x={x}"
        return None, (f"package {package!r} not among "
                      f"{[p for _, p in groups[0]]}")

    # Several families: the family label must decide it.
    for gi, grp in enumerate(groups):
        label = labels[gi] if gi < len(labels) else ""
        norm = re.sub(r"[^A-Z0-9]", "", label.upper())
        if want_fam and want_fam in norm:
            for x, pkg in grp:
                if hit(pkg):
                    return x, (f"family group {label.split(',')[0].strip()!r}, "
                               f"column {pkg} at x={x}")
            return None, (f"family {family!r} matched group {gi} but it has no "
                          f"{package!r} column")
    return None, (f"{len(groups)} device-family groups on the page and none "
                  f"matches family {family!r} -- refusing to guess, because the "
                  f"wrong column gives a wrong pin table that looks right "
                  f"(labels seen: {labels[:3]})")


def extract_multicolumn(text, family, package, pages_wanted=None,
                        name_rx=None, type_rx=None):
    """-> (pins, meta) for an ST-style multi-package pin table."""
    name_rx = name_rx or re.compile(
        r"^(?:P[A-Z]\d{1,2}|V[A-Z]{2,}[0-9+\-]*|NRST|BOOT\d|PDR_ON|VREF[+\-]?|"
        r"OSC[0-9_A-Z]*)$")
    type_rx = type_rx or re.compile(r"^(?:I/O|I|O|S|NC)$")

    pages = text.split("\f")
    idxs = pages_wanted or range(1, len(pages) + 1)
    pins = {}
    meta = {"column_x": None, "why": "", "pages": [], "rows": 0,
            "columns_seen": {}, "outlier_pages": [], "skipped": 0,
            "wrapped_pins": []}

    # PASS 1 -- decide the column, by consensus across the table's pages.
    # A 300-page datasheet mentions "LQFP100" in package-drawing and ordering
    # chapters too, and those stray pages produce a plausible-looking column at
    # the wrong offset. The real table spans dozens of pages that all agree, so
    # the modal offset is the table and everything else is noise. Taking the
    # last page's answer instead would silently mix two packages together.
    candidates = []
    for i in idxs:
        if not (1 <= i <= len(pages)):
            continue
        page = pages[i - 1]
        # TWO, not four: Microchip's MCP9701 pin-function table has exactly
        # three package columns (SC70 / SOT-23 / TO-92), so a threshold of four
        # excluded the only page that mattered and left the reader working from
        # a package-marking page whose columns do not line up -- zero pins from
        # a perfectly well-formed table.
        if len(_columns_from_headers(page)) < 2:
            continue
        x, why = choose_column(page, family, package)
        if x is None:
            meta["skipped"] += 1
            continue
        candidates.append((i, x, why))
        meta["columns_seen"].setdefault(x, []).append(i)

    if not candidates:
        meta["why"] = "no page carries a multi-package pin table for this part"
        return pins, meta

    best_x = max(meta["columns_seen"], key=lambda x: len(meta["columns_seen"][x]))
    meta["column_x"] = best_x
    meta["why"] = next(w for _, x, w in candidates if x == best_x)
    meta["outlier_pages"] = sorted(i for x, v in meta["columns_seen"].items()
                                   if x != best_x for i in v)

    # PASS 2 -- read rows only from the pages that agree with the consensus.
    for i, x, _ in candidates:
        if x != best_x:
            continue
        meta["pages"].append(i)
        # A real table row carries exactly one number cell per package column.
        # Requiring that is what stops a stray line elsewhere on the page from
        # contributing a pin number with somebody else's name attached -- a
        # WRONG name is far more dangerous than a missing one, because it is
        # indistinguishable from a correct one downstream.
        # One row carries a number cell for EVERY package column on the page,
        # including those of a second device-family group, so this is the total
        # header count and not the chosen group's width.
        #
        # KNOWN GAP: a stray package name in body text inflates this and then
        # every row fails the structural test. That is why Microchip's MCP9701
        # (SC70 / SOT-23 / TO-92, with "SOT-23" repeated in prose below the
        # table) extracts nothing. Deriving the count from the data instead was
        # tried and is worse -- it varies page to page on ST and cost 24 pins
        # there. Left as a gap rather than traded for a regression.
        ncols = len(_columns_from_headers(pages[i - 1]))
        for ln in pages[i - 1].split("\n"):
            cells = [(m.start(), m.group(0)) for m in re.finditer(r"\S+", ln)]
            if len(cells) <= ncols:
                continue
            # STRUCTURAL row test: exactly ncols number-like cells, then the
            # name. Requiring a TYPE cell after the name is what made
            # Microchip's MCP9701 table unreadable -- its fourth column is a
            # prose Function description, not a type abbreviation.
            nums = cells[:ncols]
            if not all(_PKG_CELL.fullmatch(c[1]) for c in nums):
                continue
            best = min(nums, key=lambda c: abs(c[0] - best_x))
            if abs(best[0] - best_x) > 4 or best[1] in ("-", "—", "NC"):
                continue
            name = cells[ncols][1]
            if not re.fullmatch(r"[A-Z][A-Z0-9_+\-/.()]{0,23}", name):
                continue
            if name.upper() in ("PIN", "NAME", "NO", "SYMBOL", "TYPE"):
                continue
            # When a row's NAME cell wrapped onto other lines, the first cell
            # after the numbers is the TYPE column -- and taking it as the name
            # records "I/O" or "S" as the pin's name. The STM32 acquired ten of
            # those, and a wrong name is worse than a missing one because it
            # reads downstream as a real disagreement with the symbol.
            # recover_wrapped_names() handles these rows properly.
            # NOT "NC": no-connect is a real pin NAME as well as a type
            # marker, and excluding it cost Microchip's MCP9701 pins 1 and 5.
            # The rest are type codes that no vendor uses as a pin name.
            if re.fullmatch(r"I/O|IO|I|O|S|P|FT|FT_[a-z]+|TT|TC|B", name):
                continue
            pins[best[1]] = name
            meta["rows"] += 1

        # Rows whose name cell wrapped render no name on the row line at all.
        # Recover those separately rather than losing the pin.
        wrapped = recover_wrapped_names(pages[i - 1], ncols, best_x)
        for pin, nm in wrapped.items():
            # The row's own line may hold only the middle of its name
            # ("OSC_I" of PH0-OSC_IN (PH0)); the reassembled name replaces it
            # when it contains it.
            own = pins.get(pin)
            if own is None or (own != nm and own in nm):
                pins[pin] = nm
                # Reassembled from fragments, so it can be damaged in ways a
                # normal cell cannot: pdftotext drops or splits characters in
                # hyphenated names. Flagged, not trusted -- these are the lines
                # a reviewer should read first.
                meta.setdefault("wrapped_pins", []).append(pin)
    return pins, meta


def _structural_rows(page, ncols, col_x):
    """-> [(line, pin_or_None, name_or_None, name_x)] for EVERY table row.

    Rows whose cell in the chosen column is "-" are kept, with pin=None. They
    are not pins of this package, but they still OWN their name text, and
    knowing that is what stops that name from drifting onto a neighbouring pin:
    on page 79 of DS12023, PF0 is absent from LQFP100 yet its name sits four
    lines from pin 9 and would otherwise be appended to it.
    """
    out = []
    for n, ln in enumerate(page.split("\n")):
        cells = [(m.start(), m.group(0)) for m in re.finditer(r"\S+", ln)]
        if len(cells) < ncols:
            continue
        nums = cells[:ncols]
        if not all(_PKG_CELL.fullmatch(c[1]) for c in nums):
            continue
        pick = min(nums, key=lambda c: abs(c[0] - col_x))
        if abs(pick[0] - col_x) > 4:
            continue
        rest = cells[ncols:]
        name = rest[0][1] if rest else None
        name_x = rest[0][0] if rest else None
        if name and re.fullmatch(r"I/O|I|O|S", name):
            name, name_x = None, None      # the type column, not a name
        absent = pick[1] in ("-", "—", "NC")
        out.append((n, None if absent else pick[1], name, name_x))
    return out


def _name_column_x(rows):
    """Where the pin-name column starts, from the rows that DID render one."""
    xs = [x for _, _, nm, x in rows if nm and x is not None]
    return min(xs) if xs else None


def recover_wrapped_names(page, ncols, col_x, tol=12):
    """-> {pin_number: name} for rows whose NAME wrapped onto other lines.

    ST renders the pin-name cell vertically CENTRED on its row, so a long name
    like PC14-OSC32_IN (PC14) is emitted by pdftotext as a stack of fragments
    straddling the number line:

        PC14-         <- fragment, 8 lines above the row
        OSC32
        8  D1 ...     <- the row itself, name column empty
        _IN           <- fragments below
        (PC14)

    and a name of an odd number of lines keeps its middle line on the row:
    "PH0-" / "12 ... OSC_I" / "N (PH0)".

    Each fragment goes to the nearest row, never crossing another. Centring
    decides the rest: a row that printed part of its name on its own line has
    fragments on BOTH sides, so one that would only gain them on one side is
    not their owner and passes them to its neighbour. That is what stops pin 7
    (PC13) swallowing the "PC14-" two lines below it, eight lines from its true
    owner. Rows marked "-" (absent from this package) still own their text, so
    PF0's name cannot drift onto pin 9.

    Returns only rows whose name was assembled from fragments.
    """
    lines = page.split("\n")
    rows = _structural_rows(page, ncols, col_x)
    if not rows:
        return {}
    name_x = _name_column_x(rows)
    if name_x is None:
        return {}
    row_at = {r[0]: r for r in rows}
    row_lines = sorted(row_at)
    # The rotated column headers ("Pin name", "(function after reset)") sit in
    # the same x band as the pin names, ABOVE every data row. Without this
    # bound they are absorbed as fragments: pin 76 came out as
    # "(functionafterPinname)" instead of PA14. But the first row's own name
    # starts above it too ("PA14" / "(JTCK/" over pin 76), so close above the
    # first row only upper-case text counts: heading words are lower case.
    first_row = row_lines[0]

    frags = {}
    for n, ln in enumerate(lines):
        if n in row_at or n < first_row - 8:
            continue
        if n < first_row and re.search(r"[a-z]", ln):
            continue
        # The cell is centred horizontally too, so a fragment wider than the
        # shortest name starts left of it: "(JTMS/" sits 7 left of "PA12".
        text = _band_text(ln, name_x - 8, name_x + tol)
        if text:
            frags[n] = text

    def neighbours(n):
        above = [r for r in row_lines if r < n]
        below = [r for r in row_lines if r > n]
        return (above[-1] if above else None), (below[0] if below else None)

    def needy(r):
        return row_at[r][2] is None

    owner = {}
    for n in frags:
        up, down = neighbours(n)
        cands = [c for c in (up, down) if c is not None]
        if not cands:
            continue
        # Nearest first; on a tie the row with no name of its own.
        cands.sort(key=lambda c: (abs(c - n), not needy(c)))
        owner[n] = cands

    # A row with its own middle line must gain fragments on both sides.
    for _ in range(2):
        claimed = {}
        for n, cands in owner.items():
            claimed.setdefault(cands[0], []).append(n)
        for r, ns in claimed.items():
            if needy(r):
                continue
            if any(n < r for n in ns) and any(n > r for n in ns):
                continue
            for n in ns:
                if len(owner[n]) > 1:
                    owner[n] = owner[n][1:]
                else:
                    del owner[n]

    parts = {}
    for n, cands in owner.items():
        parts.setdefault(cands[0], []).append(n)
    out = {}
    for r, ns in parts.items():
        line_no, pin, own, _ = row_at[r]
        if pin is None:
            continue
        pieces = [(n, frags[n]) for n in ns]
        if own:
            pieces.append((line_no, own))
        name = ""
        for _, t in sorted(pieces):
            name += (" " if t.startswith("(") and name else "") + t
        out[pin] = name
    return out


def _band_text(line, lo, hi):
    """The text of a line that falls in the name column: the words starting
    inside [lo, hi], and words joined to them by single spaces ("N (PH0)")."""
    words = [(m.start(), m.group(0)) for m in re.finditer(r"\S+", line)]
    out, last_end = [], None
    for x, w in words:
        if lo <= x <= hi or (out and x == last_end + 1):
            out.append(w)
            last_end = x + len(w)
        elif out:
            break
    text = " ".join(out)
    # A lone ")" is the end of a name; other punctuation is not name text.
    if not text or re.fullmatch(r"I/O|I|O|S|NC|FT|FT_[a-z]+|TT|TC|-|[^A-Za-z0-9)]+",
                                text):
        return ""
    return text


# ---------------------------------------------------------------------------
# Bus role
# ---------------------------------------------------------------------------
#
# Whether a device is an SPI controller or peripheral decides what its serial pins are
# called from the board's point of view:
#
#     peripheral:  DIN = MOSI     DOUT = MISO
#     controller:  DIN = MISO     DOUT = MOSI
#
# So a tool that equated DIN with MOSI unconditionally would bless a genuinely
# wrong symbol on any controller. The role is not assumed here -- it is READ from
# the datasheet, because a device whose chip-select and clock are INPUTS is
# being selected and clocked by somebody else, which is what being a peripheral
# means. Where the document does not say, the role stays unknown and the
# matching phase asks a human rather than guessing.

_ROLE_LINE = re.compile(
    r"^\s*(?:[0-9A-Z]{1,4}\s+)?(?P<name>CS|CSB|CS_N|NCS|SS|SCLK|SCK|SPC)\b"
    r"[^\n]{0,80}?\b(?P<dir>input|output|in|out)\b", re.I | re.M)


def spi_role(text):
    """-> ('peripheral'|'controller'|'', evidence). Read, never assumed."""
    votes = {"peripheral": [], "controller": []}
    for m in _ROLE_LINE.finditer(text):
        name, d = m.group("name").upper(), m.group("dir").lower()
        role = "peripheral" if d.startswith("in") else "controller"
        line = " ".join(m.group(0).split())[:70]
        if len(votes[role]) < 3:
            votes[role].append(f"{name} is an {d} ({line})")
    if votes["peripheral"] and not votes["controller"]:
        return "peripheral", "; ".join(votes["peripheral"][:2])
    if votes["controller"] and not votes["peripheral"]:
        return "controller", "; ".join(votes["controller"][:2])
    return "", ("the document describes these pins in both directions"
                if votes["peripheral"] and votes["controller"] else
                "no chip-select or clock direction found")


# A pin label as it appears in a package DRAWING: short, uppercase, often with
# an index. Deliberately narrow -- this is used to quote the drawing back to a
# human, not to build a table.
_DRAW_LABEL = re.compile(r"^(?:[A-Z]{1,4}[0-9]{0,2}|V[A-Z]{2,4}|GND|NC|N\.C\.)$")


def drawing_labels(text, want=0, window=3):
    """-> [(line, [labels])] for lines that look like a row of drawing labels.

    Datasheets with no pin TABLE still print the pin names in the package
    drawing, laid out in rows:

        D2    G1      S1
        S2    G2      D1

    Those names are worth quoting to whoever has to type the table -- it saves
    the transcription. What is NOT recoverable from text is which row is pins
    1..3: SOT-363 numbers from the bottom-left, and a text dump has no idea
    which row was the bottom. So this returns the LABELS ONLY, and the caller
    must present them as something to assign numbers to, never as a mapping.
    """
    out = []
    for page in text.split("\f"):
        for ln in page.split("\n"):
            toks = [m.group(0) for m in re.finditer(r"\S+", ln)]
            if not (2 <= len(toks) <= 8):
                continue
            if not all(_DRAW_LABEL.match(t) for t in toks):
                continue
            # Labels in a drawing are separated by whitespace runs, not single
            # spaces -- that is what separates a pin row from a prose fragment
            # like "AEC Q101". Two or more spaces between every pair.
            if not re.fullmatch(r"\s*\S+(?:\s{2,}\S+)+\s*", ln):
                continue
            out.append((" ".join(toks), toks))
    # Keep only labels that recur, and prefer rows whose length divides the pin
    # count -- a 6-pin part drawn as two rows of three.
    seen, keep = set(), []
    for line, toks in out:
        key = tuple(sorted(toks))
        if key in seen:
            continue
        seen.add(key)
        if want and len(toks) not in (want, want // 2, want // 3, want - 1):
            continue
        keep.append((line, toks))
    return keep[:6]
