#!/usr/bin/env python3
"""Decode an order code using the datasheet's OWN ordering-information table.

WHY THIS MATTERS. An MPN already encodes the package, and until now the tool had
to be TOLD it (`--package LQFP100`) — which is both tedious and a place to make
a silent mistake, since the wrong package selects the wrong column of an ST pin
table and yields a complete, confident, wrong pin map.

It also closes a validation gap. `STM32L4R5VIT6` says V = 100 pins and T = LQFP.
The board's footprint says `Package_QFP:LQFP-100_14x14mm_P0.5mm` and the symbol
has 100 pins. Those are three independent statements about the same fact, and
until they are compared, nothing catches a 100-pin part on a 144-pin land.

NO VENDOR TABLES ARE HARDCODED. Datasheets publish the decoding scheme —

    Example:            STM32   L   4Rx   V   I   T   6  P  TR
    Pin count
    V = 100 pins
    Q = 132 balls
    ...
    Package
    T = LQFP
    I = UFBGA (7 x 7 mm)

— so the scheme is parsed from the document and applied to the MPN. A tool that
shipped its own copy of every vendor's ordering scheme would be wrong within a
year and wrong silently.
"""
import re

# The example line names the segments in order: "STM32 L 4Rx V I T 6 P TR"
_EXAMPLE = re.compile(r"^\s*Example\s*:\s*(?P<code>.+?)\s*$", re.M | re.I)
# A section heading followed by "CODE = meaning" lines.
# Up to 6 characters: the device-family code is "STM32". Capping at 4 drops it,
# and then a later section happily consumes the leading "S" instead.
_ENTRY = re.compile(r"^\s{0,40}([A-Za-z0-9]{1,6})\s*=\s*(\S.*?)\s*$", re.M)

_SECTION_WORDS = ("device family", "product type", "device subfamily",
                  "sub-family", "pin count", "flash memory size", "package",
                  "temperature range", "option", "packing", "core")


def parse_scheme(text):
    """-> [(section_name, {code: meaning})] in document order, or [].

    The phrase appears in the table of CONTENTS long before the table itself, so
    every occurrence is tried and the first one carrying real section headings
    wins. Taking the first match returns an empty scheme from the contents page.
    """
    for m in re.finditer(r"ordering information scheme", text, re.I):
        got = _scheme_at(text, m.start())
        if got:
            return got
    return []


def _scheme_at(text, start):
    seg = text[start:start + 6000]

    # Split on the known section headings, keeping order.
    positions = []
    for word in _SECTION_WORDS:
        for hit in re.finditer(rf"^\s*({re.escape(word)})\s*$", seg,
                               re.M | re.I):
            positions.append((hit.start(), hit.group(1).strip()))
    positions.sort()
    if not positions:
        return []

    out = []
    for i, (pos, name) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(seg)
        body = seg[pos:end]
        codes = {}
        for e in _ENTRY.finditer(body):
            code, meaning = e.group(1), e.group(2)
            # "TJmax = 82 °C" style noise: a code is short and alphanumeric.
            if len(code) <= 6 and not meaning.lower().startswith(("stm32l4rxxx",)):
                codes[code.upper()] = meaning
        if codes:
            out.append((name.lower(), codes))
    return out


def decode(mpn, scheme):
    """-> ([(section, code, meaning)], leftover). Greedy, left to right.

    Position disambiguates codes that repeat between sections -- on the STM32
    scheme "I" is both a flash size and a package -- which is exactly why the
    segments must be consumed in the document's own order rather than searched
    for individually.
    """
    rest = mpn.upper().strip()
    got = []
    for name, codes in scheme:
        # longest code first, so "4R5" wins over "4"
        for code in sorted(codes, key=len, reverse=True):
            if rest.startswith(code):
                got.append((name, code, codes[code]))
                rest = rest[len(code):]
                break
        else:
            # A section that does not match is only tolerable AFTER the code has
            # started matching -- trailing option/packing fields are genuinely
            # optional. Tolerating it before that lets a late section swallow
            # the family prefix and "decode" STM32L4R5VIT6 as option "S".
            if not got:
                return [], mpn.upper().strip()
    return got, rest


def package_of(decoded):
    """-> (package_family, pin_count) from a decoded order code, or (None, 0)."""
    fam, pins = None, 0
    for name, _code, meaning in decoded:
        if name == "package" and fam is None:
            m = re.match(r"([A-Za-z]{2,10})", meaning.strip())
            if m:
                fam = m.group(1).upper()
        if name == "pin count" and not pins:
            m = re.search(r"(\d{2,3})\s*(?:pins?|balls?)", meaning, re.I)
            if m:
                pins = int(m.group(1))
    return fam, pins


def normalise_footprint(fp):
    """-> (family, pin_count) from a KiCad footprint name.

    'Package_QFP:LQFP-100_14x14mm_P0.5mm' -> ('LQFP', 100)
    'Package_SO:TSSOP-16_4.4x5mm_P0.65mm' -> ('TSSOP', 16)
    """
    name = str(fp).partition(":")[2] or str(fp)
    m = re.match(r"([A-Za-z]{2,10})[-_]?(\d{1,3})", name)
    if not m:
        return None, 0
    return m.group(1).upper(), int(m.group(2))


# Package families a vendor and KiCad may spell differently but which are the
# same land pattern. Kept deliberately tiny: a generous alias table would start
# excusing real mismatches.
_ALIAS = {"UFBGA": "BGA", "LFBGA": "BGA", "TFBGA": "BGA", "UBGA": "BGA",
          "VFQFPN": "QFN", "UFQFPN": "QFN", "WLCSP": "CSP"}


def _fam(x):
    x = (x or "").upper()
    return _ALIAS.get(x, x)


def check_package(mpn, text, footprint, symbol_pins):
    """Cross-check MPN vs footprint vs symbol -> (target, findings).

    target is the package string the pin-table reader should use (e.g.
    'LQFP100'), or None when the scheme could not be decoded -- in which case
    the caller must ask rather than assume.
    """
    findings = []
    scheme = parse_scheme(text)
    if not scheme:
        return None, [{"code": "ordering_scheme_absent", "msg":
                       "this datasheet has no machine-readable ordering scheme; "
                       "the package cannot be derived from the MPN"}]

    decoded, leftover = decode(mpn, scheme)
    fam, pins = package_of(decoded)
    if not fam or not pins:
        return None, [{"code": "ordercode_undecoded", "msg":
                       f"decoded {mpn!r} as "
                       f"{'/'.join(c for _, c, _ in decoded) or '(nothing)'}"
                       f" but could not read a package and pin count from it"}]

    fp_fam, fp_pins = normalise_footprint(footprint)
    if fp_fam and _fam(fp_fam) != _fam(fam):
        findings.append({"code": "package_mismatch", "msg":
                         f"MPN {mpn} decodes to {fam} ({pins} pins) but the "
                         f"footprint is {footprint!r} ({fp_fam})"})
    if fp_pins and fp_pins != pins:
        findings.append({"code": "pin_count_mismatch", "msg":
                         f"MPN {mpn} decodes to {pins} pins but the footprint "
                         f"{footprint!r} has {fp_pins}"})
    if symbol_pins and symbol_pins != pins:
        findings.append({"code": "symbol_pin_count_mismatch", "msg":
                         f"MPN {mpn} decodes to {pins} pins but the symbol has "
                         f"{symbol_pins}"})
    return f"{fam}{pins}", findings


# The orderable-parts table of a TI-style datasheet: one line per order code,
# carrying the package, its designator and the pin count, then the reel size.
#     TPS7A2033PDBVR  Active  Production  SOT-23 (DBV) | 5   3000 | LARGE T&R
#     TPS7A2033PDBVR     SOT-23   DBV     5     3000      178.0 ...
_ORDERABLE = re.compile(
    r"\b[A-Z][A-Z0-9\-]{1,10}\s*(?:\((?P<d1>[A-Z0-9]{2,5})\)\s*\|\s*"
    r"|\s+(?P<d2>[A-Z0-9]{2,5})\s+)(?P<n>\d{1,3})\s+\|?\s*\d{3,5}\b")
# "Plastic surface-mounted package; 6 leads" on the order code's own line.
_LEADS_ON_LINE = re.compile(r"\b(\d{1,3})\s+(?:leads|pins|balls|terminals)\b",
                            re.I)
# "8-Pin HVSSOP", "5-Lead SC70", "8 Lead TDFN Package": a count naming a package.
_COUNT_PKG = re.compile(r"\b(\d{1,3})[- ](?:Pin|Lead|Ld|Ball|Contact)s?\s+"
                        r"(?:Plastic\s+)?(?P<pkg>[A-Z][A-Za-z]*[- ]?\d{0,3}[A-Z]?)\b")


def document_pin_count(mpn, text, footprint=""):
    """-> (pins, evidence) from the datasheet's own words, or (0, why not).

    Tried in order of how directly the document ties the count to THIS order
    code: a line of the orderable-parts table that starts with the MPN, then a
    package description ("5-Lead SC70") naming the package the footprint is.
    Every line that answers must agree; two answers means none.
    """
    up = mpn.upper().strip()
    counts, where = set(), ""
    for ln in text.split("\n"):
        toks = ln.split()
        # The order code may carry a lot suffix: "TPS7A2033PDBVR.A".
        if not toks or toks[0].upper().split(".")[0] != up:
            continue
        rest = ln[ln.upper().index(up) + len(up):]
        m = _ORDERABLE.search(rest)
        if m and (m.group("d1") or m.group("d2") or "") in up:
            counts.add(int(m.group("n")))
            where = "orderable-parts table"
            continue
        m = _LEADS_ON_LINE.search(rest)
        if m:
            counts.add(int(m.group(1)))
            where = "order code's package line"
    counts = {n for n in counts if 2 <= n <= 600}
    if len(counts) == 1:
        return counts.pop(), where
    if len(counts) > 1:
        return 0, f"the order code's lines give {sorted(counts)} pins"

    fp = re.sub(r"[^A-Z0-9]", "", (str(footprint).partition(":")[2]
                                   or str(footprint)).upper())
    found = set()
    for m in _COUNT_PKG.finditer(text):
        pkg = re.sub(r"[^A-Z0-9]", "", m.group("pkg").upper())
        if len(pkg) >= 4 and fp and pkg in fp:
            found.add(int(m.group(1)))
    found = {n for n in found if 2 <= n <= 600}
    if len(found) == 1:
        return found.pop(), "package description"
    return 0, ("package descriptions disagree" if found
               else "no count found in the document")


_PINOUT_HEAD = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?\s+)?(?:PIN\s+CONFIGURATIONS?|"
                          r"Pin\s+Configurations?(?:\s+and\s+Functions)?|"
                          r"Pinout|Pin\s+Assignments?)\s*$", re.M | re.I)
# "VCIN 1" or "27 MODE": a pin label beside its number, one space apart.
_LABEL_NUM = re.compile(r"(?<!\S)(?:(\d{1,3}) [A-Za-z]\w*|[A-Za-z]\w* (\d{1,3}))(?!\S)")


def drawing_pin_count(text):
    """-> (pins, evidence) from a pinout drawing that labels EVERY pin, else 0.

    Vishay's SiC473 is an "MLP55-27L" with three exposed pads numbered 28-30,
    so neither the package name nor the order code gives 30. Its pin
    configuration drawing does: "VCIN 1", "27 MODE", "30 PGND". The count is
    taken only when the labels run 1..N without a gap; a drawing of several
    packages, or a partial one, gives no answer."""
    found = set()
    for m in _PINOUT_HEAD.finditer(text):
        seg = text[m.end():m.end() + 12000]
        cap = re.search(r"^\s*(?:Fig\.|Figure)\s", seg, re.M)
        if cap:
            seg = seg[:cap.start()]
        nums = {int(a or b) for a, b in _LABEL_NUM.findall(seg)}
        nums = {n for n in nums if 1 <= n <= 600}
        if len(nums) >= 3 and nums == set(range(1, max(nums) + 1)):
            found.add(max(nums))
    if len(found) == 1:
        return found.pop(), "pinout drawing's labels"
    return 0, ("pinout drawings disagree" if found
               else "no pinout drawing labels every pin")
