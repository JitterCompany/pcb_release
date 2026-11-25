#!/usr/bin/env python3
"""Reference pin tables — the independent evidence a symbol is checked against.

THE POINT. A symbol's pin table is a CLAIM about a manufacturer part. ERC cannot
test that claim: if the symbol says pin 14 is VDD, ERC happily confirms pin 14 is
on a power rail. Only a source outside the symbol can disagree. This module holds
that source and performs the disagreement.

WHY WE STORE THE TABLE AT ALL. The attestation design says "do not store the pin
table -- a copy of the thing under suspicion validates nothing", and that is
right about a copy taken FROM THE SYMBOL. A table transcribed from the DATASHEET
is the opposite: it is the only artifact that can contradict the symbol. It also
gives the one thing a signature cannot -- somewhere to record that the datasheet
calls a pin S1A while the symbol calls it S1_A, which is a reconciliation, not a
defect.

Deleting reference/ later is a supported operation: the attestation hash is
computed from the symbol, not from these files, so the sign-offs survive intact
and only the machine-check is lost.

FILE LAYOUT — keyed by DOCUMENT, then by PACKAGE VARIANT. One datasheet covers
many orderable numbers, and the pin table varies by package INSIDE one document
(TMUX1108PW and TMUX1108RSV are the same die, same datasheet, different pin
numbering). Keying by MPN alone duplicates; keying by document alone is wrong.
"""
import os
import re

import tomlite

# Names that mean "this pin has no functional name", per KiCad convention.
# Names that all mean "nothing is attached here". A datasheet writes N.C. or
# RESERVED; a symbol usually writes nothing at all.
_ANON = {"~", "", "-", "NC", "N/C", "N.C.", "NC.", "DNC", "RESERVED", "RSVD"}


class Variant:
    """One package variant of one document: the pin table plus who it covers."""

    __slots__ = ("doc", "part", "package", "package_code", "mpns", "mpn_match",
                 "pins", "alias", "source", "confidence", "ordering_check",
                 "active_low", "pin_count", "polarity_known", "bus_role",
                 "numbered", "polarity", "marked", "joined", "dots",
                 "pad_layout", "symbol_checked", "mark", "symbols",
                 "alias_for")

    def __init__(self, doc, d):
        self.doc = doc
        # One document can describe several DIFFERENT PARTS with different pin
        # tables -- SCDS477A covers both TMUX1308A and TMUX1309A in the same
        # TSSOP-16. `part` is what makes a variant readable; `mpns` is what
        # makes it match.
        self.part = str(d.get("part", "")).strip()
        self.package = str(d.get("package", "")).strip()
        self.package_code = str(d.get("package_code", "")).strip()
        self.mpns = [str(m).strip() for m in (d.get("mpns") or [])]
        # An interface table is keyed to symbols (lib:name), not to MPNs.
        self.symbols = [str(m).strip() for m in (d.get("symbols") or [])]
        self.mpn_match = str(d.get("mpn_match", "")).strip()
        self.source = str(d.get("source", "")).strip()
        self.confidence = str(d.get("confidence", "")).strip()
        self.ordering_check = d.get("ordering_check") or {}
        # Pin keys arrive as strings from TOML; keep them that way -- pin "0" and
        # pin "A1" (BGA) must both work, so never coerce to int.
        self.pins = {str(k).strip(): str(v).strip()
                     for k, v in (d.get("pins") or {}).items()}
        # A connector or choke datasheet numbers its pins and names none of
        # them. Such a table claims the numbering only, so it lists numbers.
        # A polarity table claims what the marking on the part means, not pin
        # names; the pins are checked through the symbol and the footprint.
        self.polarity = "marked" in d
        self.marked = str(d.get("marked", "")).strip().lower()
        self.mark = str(d.get("mark", "")).strip()   # band, dot, plus sign..
        numbers = [str(n).strip() for n in (d.get("pin_numbers") or [])]
        self.numbered = bool(numbers) and not self.pins
        # What a numbered-only datasheet still says: which pins are connected
        # inside the part (a choke's windings), where its dots are, and
        # whether the footprint's pads are numbered as its drawing is.
        # None = not asked yet.
        j = d.get("joined")
        self.joined = None if j is None else [[str(p) for p in g] for g in j]
        self.dots = [str(p) for p in d.get("dots") or []]
        self.pad_layout = str(d.get("pad_layout", "")).strip()
        self.symbol_checked = str(d.get("symbol_checked", "")).strip()
        if self.numbered:
            self.pins = {n: "" for n in numbers}
        # Per pin: "on pin 22 the symbol may say FB". Keyed by name instead, two
        # pins the symbol calls S (a dual FET's S1 and S2) would collide.
        if d.get("alias"):
            raise ValueError(f"{doc}: [variant.alias] is the old name-keyed "
                             f"format; use [variant.symbol_alias] with pin "
                             f"numbers as keys, e.g. 22 = \"FB\"")
        self.alias = {str(k).strip(): str(v).strip()
                      for k, v in (d.get("symbol_alias") or {}).items()}
        # An alias names a symbol's pin, so it is only known to be right for
        # the symbol it was accepted on. Blocks that say which symbols they
        # are for apply to those only; [variant.symbol_alias] applies to all.
        self.alias_for = []
        for b in d.get("alias_for") or []:
            syms = [str(x).strip() for x in b.get("symbols") or []]
            if not syms:
                raise ValueError(f"{doc}: an [[variant.alias_for]] block "
                                 f"needs symbols = [\"Lib:Name\"]")
            self.alias_for.append(AliasBlock(syms, {
                str(k).strip(): str(v).strip()
                for k, v in (b.get("pins") or {}).items()}))
        # Pins the datasheet marks active-low in PROSE while printing a plain
        # name in the NAME column. TI's TMUX1309A prints "EN" in Table 5-2 and
        # "Active low logic input" in the description beside it; KiCad encodes
        # the same fact as ~{EN}. Transcribing only the NAME column silently
        # discards it -- and EN polarity is one of the real differences between
        # TMUX1109 (active high) and TMUX1309 (active low), so it is exactly the
        # kind of signal this tool must not lose.
        self.active_low = {str(x).strip() for x in (d.get("active_low") or [])}
        # Total pins the PART has, per the datasheet's package drawing -- stated
        # SEPARATELY from the pins actually transcribed, so an incomplete
        # transcription is detectable instead of silently passing.
        #
        # This is not hypothetical. Quectel's module tables are name-major with
        # one row covering several pins (VBAT_BB -> "59, 60"), and whole ranges
        # appear only in prose footnotes: "GND pins 85-112", "RESERVED pins
        # 73-84" -- 40 of 146 pins that a table parse never sees. Comparing a
        # symbol against that partial table would emit 40 bogus findings and,
        # worse, would look like a completed check.
        self.pin_count = d.get("pin_count") or 0
        # Whether this reference actually establishes polarity. Extraction reads
        # the NAME column; a datasheet marks active-low in prose beside it
        # ("Active low logic input") or with an overbar the text loses. So a
        # machine-made table knows nothing about polarity, and comparing anyway
        # asserts "the datasheet says active high" -- which it does not say.
        # It would report correctly drawn active-low CS, EN and FAULT pins as
        # mismatches.
        self.polarity_known = bool(d.get("active_low")) or bool(
            d.get("polarity_known"))
        # 'peripheral' | 'controller' | '' -- READ from the datasheet at
        # extraction time, never assumed: it decides what the serial pins are
        # called from the board's side, and backwards would bless a wrong
        # symbol. Older files say slave/master.
        role = str(d.get("bus_role", "")).strip().lower()
        self.bus_role = _ROLES.get(role, role)

    def aliases(self, symbol):
        """-> {pin: name} that apply to this symbol (`Lib:Name`)."""
        out = dict(self.alias)
        for b in self.alias_for:
            if symbol and symbol_matches(b.symbols, symbol):
                out.update(b.pins)
        return out

    def other_blocks(self, symbol):
        """Alias blocks for other symbols, when none is for this one. Their
        names were never accepted for this symbol."""
        if any(symbol and symbol_matches(b.symbols, symbol)
               for b in self.alias_for):
            return []
        return list(self.alias_for)

    def all_aliases(self):
        """-> [(pin, name)] of every alias, for whichever symbol."""
        return list(self.alias.items()) + [
            kv for b in self.alias_for for kv in b.pins.items()]

    def covers(self, mpn, decoders=None):
        """Does this variant claim the given MPN?

        Enumeration XOR decoder -- never a glob evaluated here. MPN structure
        looks semantic and is not: SMA6J13A-TR and SMA6J13CA-TR differ by one
        character and are unidirectional vs bidirectional TVS parts. Any pattern
        loose enough to be convenient merges parts this tool exists to separate,
        so patterns are expanded at authoring time into `mpns`, and a decoder --
        which must prove itself against stored test vectors -- is the only
        runtime alternative.
        """
        if self.mpns and self.mpn_match:
            raise ValueError(f"{self.doc}: variant {self.package!r} sets both "
                             f"mpns and mpn_match; they are exclusive")
        if self.mpns:
            return mpn in self.mpns
        if self.mpn_match:
            dec = (decoders or {}).get(self.mpn_match)
            if dec is None:
                raise ValueError(f"{self.doc}: unknown decoder {self.mpn_match!r}")
            return bool(dec.matches(mpn))
        return False


class AliasBlock:
    """Aliases accepted for the symbols listed."""

    __slots__ = ("symbols", "pins")

    def __init__(self, symbols, pins):
        self.symbols, self.pins = symbols, pins


def symbol_matches(entries, lib_id):
    """`Lib:Name` matches itself; a bare `Name` matches that symbol in any
    library."""
    name = lib_id.partition(":")[2]
    return any(s == lib_id or (":" not in s and s == name) for s in entries)


class Document:
    """One datasheet's extracted evidence."""

    __slots__ = ("path", "doc_id", "manufacturer", "origin", "extracted_by",
                 "reviewed_by", "reviewed_on", "variants", "interface", "spec")

    def __init__(self, path, d):
        self.path = path
        self.doc_id = str(d.get("doc_id", "")).strip()
        # An interface table: the interface's name, and the spec document
        # (a URL, or a path relative to this file) that is its reference.
        self.interface = str(d.get("interface", "")).strip()
        self.spec = str(d.get("spec", "")).strip()
        self.manufacturer = str(d.get("manufacturer", "")).strip()
        self.origin = str(d.get("origin", "")).strip()
        self.extracted_by = str(d.get("extracted_by", "")).strip()
        # The person who checked this table against the datasheet. Only a
        # signed table counts as evidence; a machine cannot sign.
        self.reviewed_by = str(d.get("reviewed_by", "")).strip()
        self.reviewed_on = str(d.get("reviewed_on", "")).strip()
        self.variants = [Variant(self.doc_id or self.interface
                                 or os.path.basename(path), v)
                         for v in (d.get("variant") or [])]


def load_dir(path):
    """-> [Document] for every *.toml under a reference directory."""
    docs = []
    if not os.path.isdir(path):
        return docs
    for name in sorted(os.listdir(path)):
        if name.endswith(".toml"):
            full = os.path.join(path, name)
            docs.append(Document(full, tomlite.load(full)))
    return docs


def find_variant(docs, mpn, footprint="", decoders=None):
    """Resolve (mpn, footprint) -> the one Variant that claims it.

    Returns (variant, error). Ambiguity is an ERROR, never a guess: one MPN can
    ship in several packages selected by order code, and picking the wrong
    variant would validate a symbol against the wrong pinout -- the exact class
    of failure this tool exists to catch.
    """
    hits = [v for d in docs for v in d.variants if v.covers(mpn, decoders)]
    if not hits:
        return None, None
    if len(hits) == 1:
        return hits[0], None
    if footprint:
        fp = _norm(footprint)
        narrowed = [v for v in hits
                    if v.package and _norm(v.package) in fp]
        if len(narrowed) == 1:
            return narrowed[0], None
    pkgs = ", ".join(sorted({v.package or "?" for v in hits}))
    return None, (f"{mpn} is claimed by {len(hits)} package variants ({pkgs}) and "
                  f"footprint {footprint or '(none)'} does not disambiguate")


def _norm(s):
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


def _norm_pin_name(s):
    """Fold the cosmetic differences between a datasheet and a symbol.

    Uppercase, strip separators, and drop an overbar/active-low marker -- KiCad
    writes ~{EN} or EN# or /EN for what a datasheet prints as EN with a bar.
    Polarity is NOT dropped as information: it is compared separately, because
    'EN active high vs active low' was one of the real differences between
    TMUX1109 and TMUX1309.
    """
    t = str(s).strip().upper()
    t = re.sub(r"^~\{(.*)\}$", r"\1", t)
    t = t.lstrip("~/!").rstrip("#")
    return re.sub(r"[\s_.\-]", "", t)


# A differential pin written the datasheet's way and the KiCad way.
#   REFN0  <->  REF0_N        REFP1 <-> REF1_P        VINN <-> VIN_N
# KiCad detects a differential pair from a _N/_P SUFFIX, so a schematic that
# wants pairs recognised must move the polarity letter to the end -- which makes
# this a naming CONVENTION forced by the tool, not a discrepancy. It is reported
# as a note so the reader can see it was understood, never as a mismatch.
_DIFF_EMBEDDED = re.compile(r"^(?P<base>[A-Z]{2,})(?P<pol>[NP])(?P<idx>\d*)$")
_DIFF_SUFFIX = re.compile(r"^(?P<base>[A-Z]{2,}?)(?P<idx>\d*)_(?P<pol>[NP])$")


def _diff_form(name):
    """-> (base, polarity, index) for either spelling, else None.

    Normalised WITHOUT dropping the underscore -- it is the very thing that
    distinguishes the two spellings, and _norm_pin_name removes it.
    """
    t = re.sub(r"[\s.\-]", "", str(name).strip().upper())
    t = re.sub(r"^~\{(.*)\}$", r"\1", t).lstrip("~/!").rstrip("#")
    for rx in (_DIFF_SUFFIX, _DIFF_EMBEDDED):
        m = rx.match(t)
        if m:
            return m.group("base"), m.group("pol"), m.group("idx")
    return None


def _same_differential(a, b):
    """True when two names are the same differential pin, spelled differently.

    Requires BOTH to parse and to agree on base, polarity AND index, so REFN0
    and REFN1 stay different -- the rule tolerates a spelling, never a pin.
    """
    fa, fb = _diff_form(a), _diff_form(b)
    return bool(fa) and fa == fb and _norm_pin_name(a) != _norm_pin_name(b)


# Active-low written two ways: KiCad draws an overbar (~{RESET}, also /RESET or
# RESET#), a datasheet often spells it with a suffix (RESET_N, RESET_B,
# RESET_L, RESETn). A bare trailing N is not a suffix: EN is not ~{E}.
_LOW_MARK = re.compile(r"^(?:~\{(?P<a>.+)\}|[/!](?P<b>.+)|(?P<c>.+)#)$")
_LOW_SUFFIX = re.compile(r"^(?P<a>.+?)_[NBL]$", re.I)
_LOW_LOWER_N = re.compile(r"^(?P<a>[A-Z0-9_]+)n$")


def _active_low(name):
    """-> (base, how) for an active-low spelling, else None."""
    t = str(name).strip()
    for rx, how in ((_LOW_MARK, "mark"), (_LOW_SUFFIX, "suffix"),
                    (_LOW_LOWER_N, "suffix")):
        m = rx.match(t)
        if m:
            base = next(g for g in m.groups() if g)
            return re.sub(r"[\s_.\-]", "", base.upper()), how
    return None


def _same_active_low(a, b):
    """True when one name marks active-low KiCad's way and the other with a
    suffix, and the base names agree."""
    fa, fb = _active_low(a), _active_low(b)
    return bool(fa and fb) and fa[0] == fb[0] and fa[1] != fb[1]


# SPI names, from the device's side and the board's. A peripheral's data input
# is the controller's output:
#
#     peripheral:  DI/SI/SDI/DIN = MOSI/COPI/PICO    DO/SO/SDO/DOUT = MISO/CIPO/POCI
#     controller:  DI/SI/SDI/DIN = MISO/CIPO/POCI    DO/SO/SDO/DOUT = MOSI/COPI/PICO
#
# Applied ONLY when the datasheet established the role. On a part that could be
# either -- an MCU -- the role reads as unknown and the difference is put to a
# human instead, which is the whole reason the role is read rather than assumed.
_ROLES = {"slave": "peripheral", "master": "controller"}
_DEVICE_IN = {"DIN", "SDI", "SI", "DI"}
_DEVICE_OUT = {"DOUT", "SDO", "SO", "DO"}
# Three spellings of the same two wires. The letter after C decides:
# CO (COPI, PICO) is the controller's output, CI (CIPO, POCI) its input.
_BOARD_TO_DEVICE = {"MOSI", "COPI", "PICO"}
_DEVICE_TO_BOARD = {"MISO", "CIPO", "POCI"}
_SPI_CLOCK = {"CLK", "SCK", "SCLK"}
_SPI_SELECT = {"CS", "SS", "NSS"}
_GROUND = {"GND", "VSS"}

# SIM card contacts as ISO 7816-2 numbers them. A SIM holder's drawing prints
# these numbers, never the signal names a symbol uses. C4 and C8 are spare
# contacts with no fixed use, so they match nothing here.
_SIM_CONTACT = {"C1": {"VCC", "VDD", "SIMVCC", "SIMVDD", "VSIM"},
                "C2": {"RST", "RESET", "SIMRST"},
                "C3": {"CLK", "SIMCLK"},
                "C5": {"GND", "VSS"},
                "C6": {"VPP", "SWP"},
                "C7": {"IO", "I/O", "DATA", "SIMIO", "SIMDATA"}}


def _unit_mixups(variant, pins, nums):
    """One unit of the symbol must be one device of the datasheet: unit A
    is all of S1 G1 D1, or all of S2 G2 D2. -> findings per mixed pin."""
    devices = {}
    for p in pins:
        unit = getattr(p, "unit", "")
        if not unit or p.num not in nums:
            continue
        m = re.fullmatch(re.escape(_norm_pin_name(p.name)) + r"(\d+)",
                         _norm_pin_name(variant.pins[p.num]))
        if m and _norm_pin_name(p.name):
            devices.setdefault(unit, {})[p.num] = m.group(1)
    owner = {}
    for unit, pins_dev in devices.items():
        for dev in pins_dev.values():
            owner.setdefault(dev, set()).add(unit)
    out = []
    for unit, pins_dev in sorted(devices.items()):
        mixed = len(set(pins_dev.values())) > 1 or any(
            len(owner[d]) > 1 for d in pins_dev.values())
        if not mixed:
            continue
        listing = ", ".join(f"{n} = {variant.pins[n]}" for n in
                            sorted(pins_dev, key=_pinsort))
        for num in sorted(pins_dev, key=_pinsort):
            sym_name = next(p.name for p in pins if p.num == num)
            out.append({"code": "pin_unit_mismatch", "pin": num,
                        "datasheet": variant.pins[num],
                        "symbol": f"{sym_name} (unit {unit})",
                        "msg": f"symbol unit {unit} has pins {listing}: one "
                               f"unit must be one device of the datasheet"})
    return out


def connection_findings(variant, symbol):
    """A numbered-only part: the datasheet's internal connections and dots
    against the symbol's drawing. `symbol` is a symgraph.Symbol or None.
    -> findings; one with code connections_unread when the drawing joins
    no pins and a person has not compared it."""
    import symgraph as SG
    if variant.joined is None or symbol is None:
        return []
    want, got = SG.partition(variant.joined), SG.partition(symbol.joined)
    show = lambda gs: ", ".join("-".join(sorted(g, key=_pinsort))
                                for g in sorted(gs, key=lambda g: _pinsort(min(g, key=_pinsort)))) or "none"
    out = []
    if want and not got:
        if variant.symbol_checked != symbol.lib_id:
            out.append({"code": "connections_unread", "pin": None,
                        "datasheet": show(want), "symbol": None,
                        "msg": f"the datasheet joins {show(want)}; bomcheck "
                               f"cannot read that from the symbol's drawing"})
    elif want != got:
        out.append({"code": "connections_differ", "pin": None,
                    "datasheet": show(want), "symbol": show(got),
                    "msg": f"inside the part the datasheet joins {show(want)}, "
                           f"the symbol draws {show(got)}"})
    if variant.dots and symbol.dots and want == got and \
            not _same_phase(variant.dots, symbol.dots, want):
        out.append({"code": "dots_differ", "pin": None,
                    "datasheet": " ".join(variant.dots),
                    "symbol": " ".join(symbol.dots),
                    "msg": f"the winding dots are at pins "
                           f"{' '.join(variant.dots)} in the datasheet and "
                           f"{' '.join(symbol.dots)} in the symbol"})
    if variant.pad_layout == "differs":
        out.append({"code": "pad_layout_differs", "pin": None,
                    "datasheet": None, "symbol": None,
                    "msg": "the footprint numbers its pads differently from "
                           "the datasheet's drawing"})
    return out


def _same_phase(a, b, windings):
    """Dots at 1 and 2 say the same as dots at 4 and 3 on windings 1-4 and
    2-3: moving every dot to the other end keeps how the windings couple."""
    if set(a) == set(b):
        return True
    other = {}
    for g in windings:
        if len(g) == 2:
            x, y = sorted(g)
            other[x], other[y] = y, x
    return all(p in other for p in a) and {other[p] for p in a} == set(b)


def _sim_card(variant):
    """A SIM holder: its pin names are the card's contact numbers."""
    names = {_norm_pin_name(v) for v in variant.pins.values()}
    return len(names & set(_SIM_CONTACT)) >= 4


def _synonym(sym, ref, role):
    """-> why two different names are the same pin, or "".

    Ground always; the SPI groups only on a part whose role the datasheet
    established. Polarity is not decided here: ~{CS} vs SS still gets its
    polarity checked like any other pin.
    """
    a, b = _norm_pin_name(sym), _norm_pin_name(ref)
    if a == b:
        return ""
    if a in _GROUND and b in _GROUND:
        return "ground, named differently"
    if not role:
        return ""
    ins = _DEVICE_IN | (_BOARD_TO_DEVICE if role == "peripheral" else _DEVICE_TO_BOARD)
    outs = _DEVICE_OUT | (_DEVICE_TO_BOARD if role == "peripheral" else _BOARD_TO_DEVICE)
    for group, what in ((_SPI_CLOCK, "SPI clock"), (ins, "SPI data into the part"),
                        (outs, "SPI data out of the part"),
                        (_SPI_SELECT, "SPI chip select")):
        if a in group and b in group:
            return f"{what}; the datasheet establishes this part as an SPI {role}"
    return ""


def _spi_candidate(sym, ref, role):
    """True if this LOOKS like the same serial pin but the role is UNKNOWN.

    Only when unknown. Where the datasheet established the role and the naming
    contradicts it -- MOSI on a controller's DIN, which is its MISO -- the
    question is settled and the answer is that the symbol is wrong, so it must
    fall through to a mismatch rather than be softened into a query.
    """
    if role:
        return False
    serial = _DEVICE_IN | _DEVICE_OUT | _BOARD_TO_DEVICE | _DEVICE_TO_BOARD
    a, b = _norm_pin_name(sym), _norm_pin_name(ref)
    return a != b and a in serial and b in serial


def _is_inverted(s):
    t = str(s).strip().upper()
    return bool(re.match(r"^~\{.*\}$", t) or t.startswith(("~", "/", "!"))
                or t.endswith("#"))


def structurally_disagrees(variant, pins):
    """True when the table and the symbol are not describing the same package.

    Name differences are a conversation; a table whose pin NUMBERS barely
    overlap the symbol's is not a reading of this part at all. Promotion has to
    block on that, or a garbled extraction becomes evidence -- as a Wuerth LED
    table did, offering pins 0 and 8 for a two-pin part.
    """
    if variant.polarity:
        return False, ""
    ref, sym = set(variant.pins), {p.num for p in pins}
    if not ref or not sym:
        return True, "one side has no pins at all"
    overlap = len(ref & sym)
    if overlap < 0.6 * min(len(ref), len(sym)):
        return True, (f"only {overlap} of {min(len(ref), len(sym))} pin numbers "
                      f"are shared -- the table is not describing this package")
    return False, ""


# Findings an alias for this symbol could have settled.
_ALIASABLE = ("pin_name_mismatch", "pin_polarity", "pin_unnamed",
              "pin_name_role_unknown")


def compare(variant, pins, symbol=""):
    """Reference table vs a symbol's pin list -> list of finding dicts.

    Findings carry the pin number and both names so a report can be read without
    opening the datasheet. `symbol` is the placed symbol (`Lib:Name`): alias
    blocks for other symbols do not apply to it.
    """
    out = _compare(variant, pins, variant.aliases(symbol))
    others = variant.other_blocks(symbol)
    for f in out:
        block = next((b for b in others if f["pin"] in b.pins), None)
        if f["code"] in _ALIASABLE and block is not None:
            f["code"] = "pin_alias_other_symbol"
            f["alias"], f["alias_for"] = block.pins[f["pin"]], block.symbols
            f["msg"] = (f"pin {f['pin']}: datasheet says {f['datasheet']!r}, "
                        f"symbol {symbol or '?'} says {f['symbol']!r}; the "
                        f"alias {f['alias']!r} was accepted for "
                        f"{', '.join(block.symbols)} only")
    return out


def _compare(variant, pins, alias):
    out = []
    sym = {p.num: p.name for p in pins}
    if variant.polarity:
        if variant.marked:
            return out
        return [{"code": "reference_unfilled", "pin": None, "datasheet": None,
                 "symbol": None, "msg": "1 of 1 pin name(s) are still blank -- "
                 "what the marking on the part means is not filled in"}]

    ref_nums = set(variant.pins)
    sym_nums = set(sym)

    # An incomplete reference is a gap in the EVIDENCE, not a defect in the
    # symbol. Report it once and stop, rather than emitting one bogus
    # "pin_extra" per untranscribed pin and calling the part checked.
    # An unfilled stub: the numbers are there and the names are not. Comparing
    # it yields one confusing mismatch per pin ("datasheet says ''"), when the
    # honest statement is that nobody has typed it in yet.
    blank = [n for n, v in variant.pins.items() if not str(v).strip()]
    if variant.numbered:
        blank = []
    if blank:
        out.append({"code": "reference_unfilled", "pin": None,
                    "datasheet": None, "symbol": None,
                    "msg": f"{len(blank)} of {len(variant.pins)} pin name(s) are "
                           f"still blank -- this is a stub waiting to be typed in "
                           f"from the package drawing"})
        # The names that are typed are still evidence: compare those, so a
        # half-typed table cannot hide a difference.
        ref_nums -= set(blank)
        sym_nums -= set(blank)

    # A partial table is still evidence for the pins it has. Say once that it
    # is partial, compare what is there, and leave the untranscribed pins out
    # rather than reporting each one as extra on the symbol.
    declared = int(variant.pin_count or 0)
    partial = bool(declared) and len(ref_nums) < declared
    if partial:
        out.append({"code": "reference_incomplete", "pin": None,
                    "datasheet": None, "symbol": None,
                    "msg": f"reference table covers {len(ref_nums)} of the "
                           f"{declared} pins this part has -- complete it before "
                           f"trusting a comparison"})
        sym_nums = sym_nums & ref_nums

    for num in sorted(ref_nums - sym_nums, key=_pinsort):
        out.append({"code": "pin_missing", "pin": num,
                    "datasheet": variant.pins[num], "symbol": None,
                    "msg": f"pin {num} ({variant.pins[num]}) is in the datasheet "
                           f"but not on the symbol"})
    for num in sorted(sym_nums - ref_nums, key=_pinsort):
        out.append({"code": "pin_extra", "pin": num,
                    "datasheet": None, "symbol": sym[num],
                    "msg": f"pin {num} ({sym[num]}) is on the symbol but not in "
                           f"the datasheet"})

    out += _unit_mixups(variant, pins, sym_nums & ref_nums)
    sim_card = _sim_card(variant)
    unit = {p.num: getattr(p, "unit", "") for p in pins}
    device = {}   # pin -> the datasheet's device number (S2 -> 2)
    for num in sorted(ref_nums & sym_nums, key=_pinsort):
        if variant.numbered:
            continue
        ref_name, sym_name = variant.pins[num], sym[num]
        # A dual FET: the symbol says S in unit A, the datasheet says S2.
        # The letter is compared here; which device is checked below.
        m = re.fullmatch(re.escape(_norm_pin_name(sym_name)) + r"(\d+)",
                         _norm_pin_name(ref_name))
        if unit.get(num) and m and _norm_pin_name(sym_name):
            device[num] = m.group(1)
            out.append({"code": "pin_name_convention", "pin": num,
                        "datasheet": ref_name, "symbol": sym_name,
                        "msg": f"pin {num}: {sym_name!r} of symbol unit "
                               f"{unit[num]} is {ref_name!r}"})
            continue
        if sim_card and _norm_pin_name(sym_name) in _SIM_CONTACT.get(
                _norm_pin_name(ref_name), ()):
            out.append({"code": "pin_name_convention", "pin": num,
                        "datasheet": ref_name, "symbol": sym_name,
                        "msg": f"pin {num}: {sym_name!r} is SIM contact "
                               f"{ref_name} (ISO 7816)"})
            continue
        # An alias is a reviewed decision for this pin and this exact name.
        if num in alias and alias[num] == sym_name:
            continue
        effective = sym_name
        if _norm_pin_name(effective) == _norm_pin_name(ref_name):
            ref_inv = _is_inverted(ref_name) or num in variant.active_low
            if variant.polarity_known and _is_inverted(sym_name) != ref_inv:
                want = "active low" if ref_inv else "active high"
                got = "active low" if _is_inverted(sym_name) else "active high"
                out.append({"code": "pin_polarity", "pin": num,
                            "datasheet": ref_name, "symbol": sym_name,
                            "msg": f"pin {num} ({ref_name}): datasheet says "
                                   f"{want}, symbol says {got} ({sym_name!r})"})
            continue
        # A datasheet often names EVERY function of a pin while the symbol
        # names the one in use: ST prints "PC14-OSC32_IN(PC14)" and TI prints
        # "GPIO3/AIN11" where a symbol says PC14 or AIN11. That is a naming
        # convention, not a wiring disagreement, so it is reported as a NOTE and
        # never as a mismatch. The subsumption must be exact on a whole
        # component -- "A3Y" is not a component of "A3", so a genuine
        # direction difference still surfaces.
        # Differential spelling, on the whole name or component by component:
        # REFN1/AIN7 vs REF1_P/AIN6 must still differ, but REFN1/AIN7 vs
        # REF1_N/AIN7 is the same pin written the way KiCad needs it.
        ref_parts = [p for p in re.split(r"[-/()\s]+", ref_name) if p]
        sym_parts = [p for p in re.split(r"[-/()\s]+", effective) if p]
        pairwise = len(ref_parts) == len(sym_parts) and bool(ref_parts)

        # Synonyms: ground always, SPI names only where the DATASHEET
        # established which end of the link this part is.
        why = _synonym(effective, ref_name, variant.bus_role) or next(
            (w for x, y in zip(sym_parts, ref_parts)
             for w in [_synonym(x, y, variant.bus_role)] if w), "")
        if why and (_synonym(effective, ref_name, variant.bus_role) or pairwise and all(
                _norm_pin_name(x) == _norm_pin_name(y)
                or _synonym(x, y, variant.bus_role)
                for x, y in zip(sym_parts, ref_parts))):
            out.append({"code": "pin_name_convention", "pin": num,
                        "datasheet": ref_name, "symbol": sym_name,
                        "msg": f"pin {num}: {sym_name!r} is {ref_name!r} ({why})"})
            continue

        # ...and where it did not, say so rather than guess.
        if pairwise and all(
                _norm_pin_name(x) == _norm_pin_name(y)
                or _spi_candidate(x, y, variant.bus_role)
                for x, y in zip(sym_parts, ref_parts)) and any(
                _spi_candidate(x, y, variant.bus_role)
                for x, y in zip(sym_parts, ref_parts)):
            out.append({"code": "pin_name_role_unknown", "pin": num,
                        "datasheet": ref_name, "symbol": sym_name,
                        "msg": f"pin {num}: {sym_name!r} would be {ref_name!r} "
                               f"if this part is an SPI peripheral, and the "
                               f"opposite if it is a controller -- the datasheet "
                               f"does not say which, so this needs a human"})
            continue

        # The whole name first: "/WP" loses its marker when split on "/".
        if _same_active_low(effective, ref_name) or pairwise and all(
                _norm_pin_name(x) == _norm_pin_name(y) or _same_active_low(x, y)
                for x, y in zip(sym_parts, ref_parts)) and any(
                _same_active_low(x, y) for x, y in zip(sym_parts, ref_parts)):
            out.append({"code": "pin_name_convention", "pin": num,
                        "datasheet": ref_name, "symbol": sym_name,
                        "msg": f"pin {num}: {sym_name!r} is {ref_name!r}, "
                               f"active-low written with an overbar instead of "
                               f"a suffix"})
            continue

        if pairwise and all(
                _norm_pin_name(x) == _norm_pin_name(y) or _same_differential(x, y)
                for x, y in zip(sym_parts, ref_parts)):
            out.append({"code": "pin_name_convention", "pin": num,
                        "datasheet": ref_name, "symbol": sym_name,
                        "msg": f"pin {num}: {sym_name!r} is {ref_name!r} with the "
                               f"polarity moved to the end, as KiCad needs for "
                               f"differential-pair detection"})
            continue

        parts = {_norm_pin_name(p) for p in re.split(r"[-/()\s]+", ref_name) if p}
        one = next((p for p in parts if p == _norm_pin_name(effective)
                    or _synonym(effective, p, variant.bus_role)), None)
        if one and len(parts) > 1:
            why = _synonym(effective, one, variant.bus_role)
            out.append({"code": "pin_name_partial", "pin": num,
                        "datasheet": ref_name, "symbol": sym_name,
                        "msg": f"pin {num}: symbol {sym_name!r} names one "
                               f"function of the datasheet's {ref_name!r}"
                               + (f" ({why})" if why else "")})
            continue
        if _norm_pin_name(sym_name) in _ANON or sym_name in _ANON:
            # A no-connect that the symbol leaves blank is AGREEMENT. Both are
            # saying the same thing -- nothing is attached to this pin -- and
            # calling it a mismatch made two otherwise-clean parts fail on a
            # pin that carries no signal.
            if _norm_pin_name(ref_name) in _ANON or ref_name in _ANON:
                continue
            out.append({"code": "pin_unnamed", "pin": num,
                        "datasheet": ref_name, "symbol": sym_name,
                        "msg": f"pin {num} is {ref_name!r} in the datasheet but "
                               f"unnamed on the symbol"})
            continue
        out.append({"code": "pin_name_mismatch", "pin": num,
                    "datasheet": ref_name, "symbol": sym_name,
                    "msg": f"pin {num}: datasheet says {ref_name!r}, symbol says "
                           f"{sym_name!r}"})
    return out


def _pinsort(n):
    """Numeric where possible, else lexical -- BGA pins are A1, B2, ..."""
    s = str(n)
    return (0, int(s), "") if s.isdigit() else (1, 0, s)


def render(doc_id, manufacturer, origin, extracted_by, variants):
    """Write a reference document as TOML text (stable key order, reviewable diff)."""
    L = [f"doc_id       = {tomlite.quote(doc_id)}",
         f"manufacturer = {tomlite.quote(manufacturer)}",
         f"origin       = {tomlite.quote(origin)}",
         f"extracted_by = {tomlite.quote(extracted_by)}",
         ""]
    for v in variants:
        L.append("  [[variant]]")
        if v.get("part"):
            L.append(f"  part         = {tomlite.quote(v['part'])}")
        L.append(f"  package      = {tomlite.quote(v['package'])}")
        if v.get("package_code"):
            L.append(f"  package_code = {tomlite.quote(v['package_code'])}")
        if v.get("mpn_match"):
            L.append(f"  mpn_match    = {tomlite.quote(v['mpn_match'])}")
        else:
            items = ", ".join(tomlite.quote(m) for m in v.get("mpns", []))
            L.append(f"  mpns         = [{items}]")
        if v.get("source"):
            L.append(f"  source       = {tomlite.quote(v['source'])}")
        if v.get("confidence"):
            L.append(f"  confidence   = {tomlite.quote(v['confidence'])}")
        if v.get("bus_role"):
            L.append(f'  bus_role     = {tomlite.quote(v["bus_role"])}'
                     f'   # READ from the datasheet, not assumed')
            if v.get("bus_role_evidence"):
                L.append(f'  # evidence: {v["bus_role_evidence"][:110]}')
        if v.get("pin_count"):
            L.append(f"  pin_count    = {v['pin_count']}"
                     f"   # from the order code or the package size, never the symbol")
        else:
            L.append("  # pin_count unknown: neither the order code nor the "
                     "footprint gave one,")
            L.append("  # so COMPLETENESS IS UNCHECKED for this part.")
        if v.get("active_low"):
            items = ", ".join(tomlite.quote(x) for x in v["active_low"])
            L.append(f"  active_low   = [{items}]"
                     f"   # datasheet marks these active-low in prose only")
        oc = v.get("ordering_check") or {}
        if oc:
            L.append("")
            L.append("    [variant.ordering_check]")
            for mpn, rec in oc.items():
                found = "true" if rec.get("found") else "false"
                tbl = tomlite.quote(rec.get("table", ""))
                L.append(f"    {mpn} = {{ found = {found}, table = {tbl} }}")
        L.append("")
        L.append("    [variant.pins]")
        for num in sorted(v["pins"], key=_pinsort):
            L.append(f"    {num} = {tomlite.quote(v['pins'][num])}")
        if v.get("alias"):
            L.append("")
            L.append("    [variant.symbol_alias]")
            for k in sorted(v["alias"], key=_pinsort):
                L.append(f"    {k} = {tomlite.quote(v['alias'][k])}")
        L.append("")
    return "\n".join(L) + "\n"
