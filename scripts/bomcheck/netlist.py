#!/usr/bin/env python3
"""Read a kicad-cli `kicadxml` netlist, carrying the SYMBOL PIN TABLE.

The existing scripts/kicad_netlist.py answers "is this part placed?" and stops
there. Pin validation needs the thing it drops: every <libpart>'s pin table.

WHY THE NETLIST AND NOT THE .kicad_sym. The netlist records the pin table the
board ACTUALLY USED, resolved by KiCad itself. Reading the symbol library
instead would (a) require sym-lib-table resolution, which in this project points
at an absolute ${KIPRJMOD}/../../../common path CI cannot pin, and (b) answer a
different question -- what the library says today, not what was placed. When a
symbol is edited after a board is laid out, those diverge, and the board is what
gets manufactured.

Multi-unit symbols: pins are collected from the whole <libpart>, never one
<unit>. A dual-FET's S/G/D numbering varies between vendors in the same SOT-363,
so a per-unit view would compare half a part.
"""
import re
import xml.etree.ElementTree as ET

# A pin whose "name" is just its own number carries no information -- KiCad
# writes "~" for an unnamed pin, and generic symbols often number-as-name.
_ANONYMOUS_NAMES = {"~", "", "-"}


class Pin:
    __slots__ = ("num", "name", "type", "unit")

    def __init__(self, num, name, type_, unit=""):
        self.num, self.name, self.type = num, name, type_
        self.unit = unit   # A, B.. on a symbol with several units; from symgraph

    def key(self):
        """The identity tuple that the pinmap hash is taken over."""
        return (self.num, self.name, self.type)

    def __repr__(self):
        return f"Pin({self.num}:{self.name}:{self.type})"


class LibPart:
    __slots__ = ("lib", "part", "pins", "footprints", "fields")

    def __init__(self, lib, part, pins, footprints, fields):
        self.lib, self.part = lib, part
        self.pins, self.footprints, self.fields = pins, footprints, fields

    @property
    def id(self):
        return f"{self.lib}:{self.part}"

    def pinmapped(self):
        """True if this symbol's pin table carries information worth attesting.

        The discriminator from the attestation design: MORE THAN 2 PINS, or any
        pin carrying a functional name rather than merely its own number. It is
        what keeps 2-pin anonymous passives out of the system entirely -- they
        have no pin table to bind to a datasheet -- with no manual per-part
        classification and no opt-in list.
        """
        if len(self.pins) > 2:
            return True
        return any(p.name not in _ANONYMOUS_NAMES and p.name != p.num
                   for p in self.pins)


# KiCad's native do-not-populate flags, plus the legacy markers this project
# used before them (see the DNP lint in scripts/dnp_lint.py).
NATIVE_SKIP = ("dnp", "exclude_from_bom", "exclude_from_pos_files")
LEGACY_VALUES = {"dni", "dnp", "logo", "mousebite", "inf"}
LEGACY_FIELDS = {"dni", "dnp"}


class Comp:
    __slots__ = ("ref", "value", "fields", "props", "footprint", "libsource", "libpart")

    def __init__(self, ref, value, fields, props, footprint, libsource):
        self.ref, self.value = ref, value
        self.fields, self.props = fields, props
        self.footprint, self.libsource = footprint, libsource
        self.libpart = None                 # joined in by parse()

    @property
    def mpn(self):
        """MPN from the fields, case-insensitively. Symbols in the wild use
        lowercase field names too ('manufacturer'), so do not trust exact
        casing."""
        for k, v in self.fields.items():
            if k.strip().lower() == "mpn" and v.strip():
                return v.strip()
        return ""

    @property
    def mpn_effective(self):
        """-> (mpn, source). Falls back to VALUE when the MPN field is empty.

        Some symbols carry the part number in Value instead (a transistor
        with Value `BC857BS` and no MPN field), and refusing to look means the
        part cannot be validated at all. But the fallback must be able to say
        NO: a connector with Value `swd_with_uart_client` has a functional
        name, and accepting that as a part number would send the tool hunting
        for a datasheet that cannot exist.

        The MPN field remains the source of truth; using Value is always
        reported, because a part number living only in Value is a gap in the
        design data even when this tool can cope with it.
        """
        if self.mpn:
            return self.mpn, "mpn-field"
        v = (self.value or "").strip()
        return (v, "value-field") if looks_like_mpn(v) else ("", "none")

    def field(self, name):
        for k, v in self.fields.items():
            if k.strip().lower() == name.lower():
                return v.strip()
        return ""

    @property
    def not_placed(self):
        """True if nothing gets soldered here, so there is nothing to validate.

        A do-not-populate part is not on the BOM and is not fitted, so demanding
        an MPN and a datasheet for it is noise: a DNI U.FL connector would be
        reported as a missing datasheet. Both the native
        KiCad flags and the legacy markers are honoured, matching the existing
        dnp_lint, so a part excluded one way is not silently validated the
        other."""
        for name in NATIVE_SKIP:
            if name in self.props:
                return True
        if self.value.strip().lower() in LEGACY_VALUES or \
                self.value.strip().lower().startswith("dni"):
            return True
        for k, v in list(self.fields.items()) + list(self.props.items()):
            if k.strip().lower() in LEGACY_FIELDS and str(v).strip():
                return True
        return False

    @property
    def pins(self):
        return self.libpart.pins if self.libpart else []

    def __repr__(self):
        return f"Comp({self.ref} {self.value!r} {self.libsource})"


_VALUE_UNIT = re.compile(
    r"^[0-9]+(?:[.,][0-9]+)?\s*(?:[pnumkKMGR]|[pnumkKMG]?(?:F|H|R|ohm|Ω|V|A|W|"
    r"Hz|kHz|MHz|%))?$", re.I)


def looks_like_mpn(v):
    """Is this string plausibly a manufacturer part number?

    Deliberately conservative -- a wrong guess sends the tool looking for a
    datasheet that does not exist, and worse, could match somebody else's part.
    Wants no whitespace, a mix of letters and digits (or a long all-digit code,
    as Wuerth uses), and rejects component values like `100n` and prose-ish
    names like `swd_with_uart_client`.
    """
    v = (v or "").strip()
    if not v or len(v) < 4 or any(ch.isspace() for ch in v):
        return False
    # A component value is short and usually carries a unit. A bare long digit
    # string is not a value -- Wuerth part numbers look like 490107670612 -- and
    # rejecting it loses a whole vendor.
    if _VALUE_UNIT.match(v) and (len(v) <= 6 or re.search(r"[A-Za-zµΩ%]", v)):
        return False
    if v.islower():                       # functional names are written lowercase
        return False
    digits = sum(ch.isdigit() for ch in v)
    letters = sum(ch.isalpha() for ch in v)
    if digits and letters:
        return True
    return digits >= 6 and not letters    # e.g. Wuerth 490107670612


def _pins_of(node):
    """Every <pin> under a node, in document order, deduplicated by number.

    Searched with .iter() so multi-unit symbols -- whose pins sit under nested
    <unit> elements -- yield the WHOLE part, not one unit.
    """
    seen, out = set(), []
    for p in node.iter("pin"):
        num = (p.get("num") or "").strip()
        if not num or num in seen:
            continue
        seen.add(num)
        out.append(Pin(num, (p.get("name") or "").strip(),
                       (p.get("type") or "").strip()))
    return out


def parse(xml_path):
    """-> (comps, libparts) with each comp joined to its libpart.

    A comp whose <libsource> names a libpart absent from the netlist keeps
    libpart=None rather than raising -- that is a real (reportable) condition,
    not a parse error.
    """
    root = ET.parse(xml_path).getroot()

    libparts = {}
    for lp in root.iter("libpart"):
        lib = lp.get("lib") or ""
        part = lp.get("part") or ""
        fields = {(f.get("name") or ""): (f.text or "").strip()
                  for f in lp.findall("fields/field")}
        # The library's own one-line description ("Polarized capacitor"):
        # a property of the symbol that a check can scope by.
        fields.setdefault("Description", (lp.findtext("description") or "").strip())
        fps = [(fp.text or "").strip() for fp in lp.findall("footprints/fp")]
        libparts[f"{lib}:{part}"] = LibPart(lib, part, _pins_of(lp), fps, fields)

    comps = []
    for c in root.iter("comp"):
        ls = c.find("libsource")
        libid = (f"{ls.get('lib') or ''}:{ls.get('part') or ''}"
                 if ls is not None else "")
        comp = Comp(
            ref=c.get("ref") or "",
            value=(c.findtext("value") or "").strip(),
            fields={(f.get("name") or ""): (f.text or "").strip()
                    for f in c.findall("fields/field")},
            props={(p.get("name") or ""): (p.get("value") or "").strip()
                   for p in c.findall("property")},
            footprint=(c.findtext("footprint") or "").strip(),
            libsource=libid,
        )
        comp.libpart = libparts.get(libid)
        comps.append(comp)

    return comps, libparts


def design_rev(xml_path):
    """The schematic's revision, from the root sheet's title block."""
    root = ET.parse(xml_path).getroot()
    for sheet in root.iter("sheet"):
        if sheet.get("name") == "/":
            return (sheet.findtext("title_block/rev") or "").strip()
    return ""


def pinmap_hash(pins):
    """sha256 over the sorted (num, name, type) tuples, truncated to 16 hex.

    Hashes ONLY the semantics, per the attestation design: moving a pin
    graphically, editing a description or changing the default footprint leaves
    this unchanged, so an attestation holds. Adding, removing, renaming or
    retyping a pin changes it, so the attestation lapses. That is what keeps the
    check from becoming a churn machine.

    Sorted by (num, name, type) as strings so the value does not depend on
    document order.
    """
    import hashlib
    payload = "\n".join(f"{n}\t{nm}\t{t}" for n, nm, t in sorted(p.key() for p in pins))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
