#!/usr/bin/env python3
"""What does each BOM line still need, and from whom?

Offline and read-only: the netlist, the reference tables and the review CSVs.
`status` prints this list, `wizard` walks it, and a CI gate can count it. So all
three agree on what "done" means.

A BOM line is DONE when a signed reference table covers every pin of its symbol
and every pin agrees, and its Datasheet field holds a URL.
"""
import glob
import os
import re

import library as LIB
import datasheet as D
import decisions as DC
import exempt as EX
import footprint as FP
import polarity as PO
import pintable as P
import tomlite
import symgraph as SG
import waivers as WV

# Order = the order a person should work in. Later kinds depend on earlier
# ones: there is no point reviewing a pin table for a part whose MPN is unset.
KINDS = [
    ("waiver_invalid", "Errors in waivers.toml"),
    ("waiver_expired", "Known issues of an earlier revision"),
    ("waiver_stale", "Accepted findings that no longer occur"),
    ("mpn_missing", "Set the MPN field"),
    ("mixed_symbols", "One part number placed with different symbols"),
    ("apply_pending", "Answers not yet written to the schematic"),
    ("datasheet_missing", "Give a datasheet, or record why there is none"),
    ("not_prepared", "Not looked at yet"),
    ("polarity_switch", "An unfinished answer from an earlier session"),
    ("stub_unfilled", "Type the pin names from the package drawing"),
    ("polarity_unfilled", "Say what the marking on the part means"),
    ("draft_review", "Check the pins read from the datasheet"),
    ("pin_difference", "Pin names that differ from the datasheet"),
    ("polarity", "Polarity: datasheet, symbol and footprint disagree"),
    ("footprint_mark", "Confirm which pad a footprint's polarity mark is at"),
    ("connections_unfilled", "Say which pins are connected inside the part"),
    ("connections_unread", "Compare the symbol's drawing with the datasheet"),
    ("connections", "Inside the part: datasheet and symbol disagree"),
    ("table_partial", "Some pins are not in the datasheet's list"),
    ("unsigned", "Pins nobody has checked yet"),
    ("interface_unset", "Pin names nothing checks: say which interface they follow"),
    ("interface_unfilled", "Type the interface's pin names from its spec"),
    ("interface_difference", "Pin names that differ from the interface spec"),
    ("interface_partial", "Some pins are not in the interface table"),
    ("interface_unsigned", "Interface names nobody has checked yet"),
]
AUTOMATIC = {"not_prepared"}

# Codes from pintable.compare that mean the symbol and the table disagree.
DISAGREE = ("pin_name_mismatch", "pin_polarity", "pin_unnamed",
            "pin_name_role_unknown", "pin_unit_mismatch")
GAPS = ("pin_missing", "pin_extra", "reference_incomplete")


class Item:
    """One open thing. `code` is the stable name a waiver refers to; `detail`
    is prose and may be reworded at any time."""
    __slots__ = ("kind", "mpn", "refs", "path", "detail", "findings", "code")

    def __init__(self, kind, mpn, refs, path="", detail="", findings=None,
                 code=""):
        self.kind, self.mpn, self.refs = kind, mpn, refs
        self.path, self.detail = path, detail
        self.findings = findings or []
        self.code = code or kind


    def __repr__(self):
        return f"<{self.kind} {self.mpn} {' '.join(self.refs)}>"


# Design findings a person may waive (see waivers.py). Missing work -- a
# table nobody typed, a signature -- is never waivable.
WAIVABLE = {"pin_difference", "interface_difference", "mixed_symbols", "connections", "polarity.unnamed_cathode",
            "polarity.wrong_pad", "polarity.polar_on_unmarked",
            "polarity.bidirectional_on_marked", "polarity.datasheet_not_polar",
            "polarity.mark_unclear",
            "polarity.datasheet_polar"}


class Line:
    """One BOM line: the placements sharing an MPN."""

    __slots__ = ("mpn", "comps", "table", "table_path", "draft", "draft_path",
                 "symbol")

    def __init__(self, mpn, comps):
        self.mpn, self.comps = mpn, comps
        self.table = self.table_path = self.draft = self.draft_path = None
        self.symbol = None   # symgraph.Symbol, when the project is known

    @property
    def refs(self):
        return sorted({c.ref for c in self.comps}, key=_refsort)

    @property
    def comp(self):
        return self.comps[0]


def _refsort(r):
    m = re.match(r"([A-Za-z_]+)(\d+)", r)
    return (m.group(1), int(m.group(2))) if m else (r, 0)


def load_docs(att, sub):
    out = []
    for f in sorted(glob.glob(os.path.join(att, "reference", sub, "*.toml"))):
        out.append((f, P.Document(f, tomlite.load(f))))
    return out


def signed_docs(att, project=None):
    """-> [(path, Document)] of signed tables: the board's own first, then the
    component libraries'. A board table for an MPN wins over a library one, so
    a project can override a part for itself."""
    own = load_docs(att, "datasheet")
    if not project:
        return own
    covered = {m for _f, d in own for v in d.variants for m in v.mpns}
    out = list(own)
    for root in LIB.read_roots(project):
        for f, d in load_docs(os.path.join(root, "attestations"), "datasheet"):
            if not {m for v in d.variants for m in v.mpns} & covered:
                out.append((f, d))
    return out


# ---- interface tables ----------------------------------------------------
# A connector that implements an interface (SL23) has two references: the
# part's datasheet, which only numbers the contacts, and the interface spec,
# which names them. The symbol says which interface with this field.
FIELD = "Interface"
INTERFACE_DRAFTS = os.path.join("draft", "interface")

# Pin names that carry no function: a connector's Pin_1, a choke's "1".
_NUMBERISH = re.compile(r"^(pin_?)?\d+$|^[~-]?$", re.I)


def numbered_only(pins):
    """True when the symbol names no pin, so the only claim is the numbering."""
    return all(_NUMBERISH.match(p.name.strip()) or p.name == p.num for p in pins)


def interface_of(comp):
    """The symbol's Interface field: on the placed symbol, else on the
    library symbol the netlist carries. "" when there is none."""
    value = comp.field(FIELD)
    lp = getattr(comp, "libpart", None)
    if not value and lp is not None:
        value = next((v.strip() for k, v in lp.fields.items()
                      if k.strip().lower() == FIELD.lower()), "")
    return value


def covers_symbol(var, lib_id):
    """An interface variant lists the symbols it is for: `Lib:Name`, or a
    bare `Name` for that symbol in any library."""
    name = lib_id.partition(":")[2]
    return any(s == lib_id or (":" not in s and s == name) for s in var.symbols)


def interface_docs(att, project=None):
    """-> [(path, Document)] of signed-place interface tables: the board's
    own first, then the component libraries'. The board's wins per symbol."""
    own = load_docs(att, "interface")
    if not project:
        return own
    covered = {s for _f, d in own for v in d.variants for s in v.symbols}
    out = list(own)
    for root in LIB.read_roots(project):
        for f, d in load_docs(os.path.join(root, "attestations"), "interface"):
            if not {s for v in d.variants for s in v.symbols} & covered:
                out.append((f, d))
    return out


def find_interface(docs, name, lib_id):
    """-> (path, Document, Variant) of the table for interface `name` that
    covers this symbol, or (None, None, None). An empty name takes any."""
    for f, d in docs:
        if name and d.interface.lower() != name.lower():
            continue
        for v in d.variants:
            if covers_symbol(v, lib_id):
                return f, d, v
    return None, None, None


def interface_draft_path(att, name, lib_id):
    safe = lambda s: re.sub(r"[^A-Za-z0-9._-]", "_", s)
    return os.path.join(att, "reference", INTERFACE_DRAFTS,
                        f"{safe(name)}-{safe(lib_id.partition(':')[2] or lib_id)}.toml")


def names_unchecked(ln):
    """A signed numbered-only part table on a symbol that names its pins
    (more than two): the datasheet checks the numbering, nothing the names."""
    c = ln.comp
    return (ln.table is not None and ln.table.numbered and len(c.pins) > 2
            and not numbered_only(c.pins))


def interface_items(ln, docs, drafts):
    """The symbol's names against its interface table, when it names an
    interface or when its datasheet only numbers the pins."""
    c = ln.comp
    name = interface_of(c)
    if not name:
        if not names_unchecked(ln):
            return []
        _f, other, _v = find_interface(docs, "", c.libsource)
        what = (f"a signed table for interface {other.interface} covers this "
                f"symbol: add the field {FIELD} = {other.interface} to the symbol"
                if other else
                f"if they follow an interface spec, add the field {FIELD} = "
                f"<name> to the symbol and sign its table with `bomcheck wizard`")
        return [Item("interface_unset", ln.mpn, ln.refs, "",
                     f"the datasheet only numbers the pins, so nothing checks "
                     f"the names of {c.libsource}; {what}")]
    path, doc, var = find_interface(docs, name, c.libsource)
    if var is None:
        dpath, _d, dvar = find_interface(drafts, name, c.libsource)
        typed = sum(1 for v in (dvar.pins.values() if dvar else []) if v.strip())
        detail = (f"{FIELD} = {name}: no signed table for {c.libsource}"
                  + (f", draft has {typed} of {len(dvar.pins)} names" if dvar
                     else "; type it from the spec in `bomcheck wizard`"))
        return [Item("interface_unfilled", ln.mpn, ln.refs, dpath or "", detail)]
    out = []
    broken, why = P.structurally_disagrees(var, c.pins)
    found = P.compare(var, c.pins)
    bad = [f for f in found if f["code"] in DISAGREE]
    if broken:
        out.append(Item("interface_difference", ln.mpn, ln.refs, path,
                        f"{name}: {why}", code="interface_structure"))
    elif bad:
        out.append(Item("interface_difference", ln.mpn, ln.refs, path,
                        f"{name}: " + _summary(bad), bad,
                        code="interface_difference"))
    gaps = [f for f in found if f["code"] in GAPS + ("reference_unfilled",)]
    if gaps and not broken:
        out.append(Item("interface_partial", ln.mpn, ln.refs, path,
                        f"{name}: " + _summary(gaps), gaps))
    if not doc.reviewed_by:
        out.append(Item("interface_unsigned", ln.mpn, ln.refs, path, name))
    return out


def lines_of(parts):
    """Group placements by MPN. A part with no MPN is its own line."""
    groups = {}
    for c in parts:
        mpn, _ = c.mpn_effective
        groups.setdefault(mpn or f"({c.ref})", []).append(c)
    return [Line(k, v) for k, v in sorted(groups.items())]


def _variant_for(docs, mpn, footprint):
    """-> (path, variant) or (None, None); ambiguity counts as none."""
    hits = [(f, v) for f, d in docs for v in d.variants if mpn in v.mpns]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        v, _err = P.find_variant([d for _f, d in docs], mpn, footprint)
        for f, hv in hits:
            if hv is v:
                return f, v
    return None, None


def pending_fields(review_dir, parts):
    """Field values in decisions-fields.csv that the schematic does not hold yet."""
    by_ref = {c.ref: c for c in parts}
    out = []
    for e in DC.parse_fields(os.path.join(review_dir, "decisions-fields.csv")):
        if e.problem:
            out.append(e)
            continue
        if any(ref in by_ref and by_ref[ref].field(e.field) != e.value
               for ref in e.refs):
            out.append(e)
    return out


def pending_pins(review_dir, promoted):
    """Accepted pin rows whose alias is not in any table yet."""
    aliases = {(m, pin, name) for _f, d in promoted for v in d.variants
               for m in v.mpns for pin, name in v.alias.items()}
    out = []
    for p in DC.parse_pins(os.path.join(review_dir, "decisions-pins.csv")):
        if p.accepted and not p.auto and \
                (p.part, p.pin, p.symbol) not in aliases:
            out.append(p)
    return out


def datasheet_state(comp, project, exemptions):
    """-> (state, detail); state is ok, exempt or missing.

    A URL, a project file or an embedded file satisfies the rule. So does an
    exemption someone signed. A file path that does not exist does not.
    """
    value = comp.field("Datasheet")
    kind = D.field_kind(value)
    detail = ""
    if kind == "file" and os.path.isabs(value.strip()):
        kind, detail = "none", (f"absolute path, works on one machine only: "
                                f"{value}. Write it as ${{KIPRJMOD}}/...")
    elif kind == "file" and project and not os.path.isfile(D.field_path(value, project)):
        kind, detail = "none", f"file not found: {value}"
    if kind != "none":
        return "ok", ""
    mpn, _ = comp.mpn_effective
    ex = exemptions.get(mpn)
    if ex and not ex.problem:
        return "exempt", ex.reason
    return "missing", (ex.problem if ex else detail)


def collect(parts, att, project=None, rev=None, waived=None):
    """-> (lines, items). `lines` carries the resolved tables for callers.
    Without `project`, file paths in Datasheet fields are not checked.
    Waived findings go to the `waived` list, if one is given, not to items."""
    promoted = signed_docs(att, project)
    drafts = load_docs(att, "draft")
    interfaces = interface_docs(att, project)
    iface_drafts = load_docs(att, INTERFACE_DRAFTS)
    review = os.path.join(att, "review")
    lines = lines_of(parts)
    items = []

    fields = pending_fields(review, parts)
    pins = pending_pins(review, promoted)
    if fields or pins:
        waiting = [f"{_count(len(n), what)} waiting in review/{csv}"
                   for n, what, csv in ((fields, "field value", "decisions-fields.csv"),
                                        (pins, "pin acceptance", "decisions-pins.csv"))
                   if n]
        items.append(Item("apply_pending", "", [], review,
                          " and ".join(waiting)))
    url_pending = {r for e in fields if e.field == "Datasheet" for r in e.refs}
    exemptions = EX.load(att)
    marks = FP.board_marks(project, att) if project else {}
    drawings = SG.load(project) if project else {}

    for ln in lines:
        c = ln.comp
        ln.symbol = drawings.get(c.libsource)
        if ln.symbol:
            units = ln.symbol.unit_of()
            for x in ln.comps:
                for p in x.pins:
                    p.unit = units.get(p.num, "")
        if ln.mpn.startswith("("):
            items.append(Item("mpn_missing", "", ln.refs))
            continue
        states = [(c, datasheet_state(c, project, exemptions)) for c in ln.comps
                  if c.ref not in url_pending]
        missing = [(c, d) for c, (st, d) in states if st == "missing"]
        if missing:
            items.append(Item("datasheet_missing", ln.mpn,
                              sorted({c.ref for c, _d in missing}, key=_refsort),
                              detail=next((d for _c, d in missing if d), "")))

        symbols = sorted({x.libsource for x in ln.comps})
        if len(symbols) > 1:
            items.append(Item("mixed_symbols", ln.mpn, ln.refs,
                              detail=", ".join(symbols)))

        ln.table_path, ln.table = _variant_for(promoted, ln.mpn, c.footprint)
        if ln.table is None:
            ln.draft_path, ln.draft = _variant_for(drafts, ln.mpn, c.footprint)
        klass = PO.classify(c)
        var = ln.table or ln.draft
        items += footprint_items(ln, marks, var)
        if klass and var is not None and not var.polarity and \
                not any(v.strip() for v in var.pins.values()):
            # A blank pin stub for a part where only polarity can be known:
            # prepare replaces it with a polarity stub.
            items.append(Item("not_prepared", ln.mpn, ln.refs))
            continue
        if klass and half_typed_pin_stub(ln):
            # Typed by hand before polarity stubs existed; prepare keeps
            # typed data, so only a person can switch it.
            items.append(Item("polarity_switch", ln.mpn, ln.refs, ln.draft_path,
                              "names half typed"))
            continue
        items += _table_items(ln, promoted)
        items += interface_items(ln, interfaces, iface_drafts)
        if var is not None and var.polarity and var.marked:
            items += datasheet_items(ln, var)

    items, done = WV.apply(items, WV.load(att), rev, WAIVABLE,
                           lambda kind, w, detail: Item(kind, "", w.refs,
                                                        WV.path_of(att), detail,
                                                        findings=[w]))
    if waived is not None:
        waived += done

    return lines, items


def _count(n, noun):
    return f"{n} {noun}" + ("" if n == 1 else "s")


def half_typed_pin_stub(ln):
    """An unsigned pin stub, typed by hand, with some names still blank."""
    var = ln.draft
    if ln.table is not None or var is None or var.polarity or var.numbered:
        return False
    names = [v.strip() for v in var.pins.values()]
    doc = P.Document(ln.draft_path, tomlite.load(ln.draft_path))
    return doc.origin == "manual" and any(names) and not all(names)


def _summary(found):
    """A few words for the status column: how much is open, not what."""
    for f in found:
        m = re.match(r"(\d+) of (\d+) pin name", f["msg"] or "")
        if f["code"] == "reference_unfilled" and m:
            return f"{m.group(1)} of {m.group(2)} blank"
        m = re.search(r"covers (\d+) of the (\d+)", f["msg"] or "")
        if f["code"] == "reference_incomplete" and m:
            return f"has {m.group(1)} of {m.group(2)}"
    n = len({f["pin"] for f in found if f["pin"]})
    return f"{n} pin(s)" if n else ""


def _table_items(ln, promoted):
    c = ln.comp
    if ln.table is None and ln.draft is None:
        return [Item("not_prepared", ln.mpn, ln.refs)]
    if ln.table is None:
        found = P.compare(ln.draft, c.pins)
        if any(f["code"] == "reference_unfilled" for f in found):
            kind = "polarity_unfilled" if ln.draft.polarity else "stub_unfilled"
            return [Item(kind, ln.mpn, ln.refs, ln.draft_path,
                         _summary(found), found)]
        broken, why = P.structurally_disagrees(ln.draft, c.pins)
        n_bad = len({f["pin"] for f in found if f["code"] in DISAGREE})
        detail = ("could not be read" if broken else
                  f"{len(ln.draft.pins)}/{len(c.pins)} read"
                  + (f", {n_bad} differ" if n_bad else ""))
        return [Item("draft_review", ln.mpn, ln.refs, ln.draft_path, detail,
                     found)]

    out = []
    broken, why = P.structurally_disagrees(ln.table, c.pins)
    found = P.compare(ln.table, c.pins)
    bad = [f for f in found if f["code"] in DISAGREE]
    if broken:
        out.append(Item("pin_difference", ln.mpn, ln.refs, ln.table_path,
                        detail=why, code="table_structure"))
    elif bad:
        out.append(Item("pin_difference", ln.mpn, ln.refs, ln.table_path,
                        _summary(bad), bad))
    gaps = [f for f in found if f["code"] in GAPS + ("reference_unfilled",)]
    if gaps and not broken:
        out.append(Item("table_partial", ln.mpn, ln.refs, ln.table_path,
                        _summary(gaps), gaps))
    out += connection_items(ln)
    doc = next(d for f, d in promoted if f == ln.table_path)
    if not doc.reviewed_by:
        out.append(Item("unsigned", ln.mpn, ln.refs, ln.table_path))
    return out


def asks_connections(var, pins):
    """A numbered-only table of a part with more than two pins: its names
    say nothing, so which pins are connected inside (a choke's windings, a
    switch's contacts) and how the pads are laid out must be asked."""
    return var.numbered and len(pins) > 2


def connection_items(ln):
    var = ln.table
    if not asks_connections(var, ln.comp.pins):
        return []
    if var.joined is None or not var.pad_layout:
        return [Item("connections_unfilled", ln.mpn, ln.refs, ln.table_path)]
    found = P.connection_findings(var, ln.symbol)
    out = []
    unread = [f for f in found if f["code"] == "connections_unread"]
    if unread:
        out.append(Item("connections_unread", ln.mpn, ln.refs, ln.table_path,
                        unread[0]["msg"], unread))
    bad = [f for f in found if f["code"] != "connections_unread"]
    if bad:
        out.append(Item("connections", ln.mpn, ln.refs, ln.table_path,
                        "; ".join(f["msg"] for f in bad), bad))
    return out


def footprint_items(ln, marks, var=None):
    """Symbol vs each placement's footprint; needs no datasheet.

    A polar part on an unmarked footprint can go on either way round, which is
    the serious case. A bidirectional part on a marked footprint is harmless
    but makes the assembler stop and ask.
    """
    wrong, unsure, unmarked, needless, unnamed = {}, {}, {}, {}, {}
    out = []
    for c in ln.comps:
        klass = PO.classify(c)
        name, pad, why, _fp = marks.get(c.ref, ("", "", "", None))
        if not klass or not name:
            continue
        confirmed_bare = why.startswith("confirmed unmarked")
        if klass == PO.BIDIRECTIONAL:
            if pad:
                needless.setdefault((name, pad), []).append(c.ref)
            continue
        marked = var.marked if var is not None and var.polarity else ""
        plus_sign = why.startswith("a + sign")
        pin = PO.marked_pin(c, klass, marked, plus_sign)
        if not pin:
            unnamed.setdefault(c.libsource, []).append(c.ref)
        elif confirmed_bare:
            unmarked.setdefault(name, []).append(c.ref)
        elif not pad:
            unsure.setdefault(name, []).append(c.ref)
        elif pad != pin:
            wrong.setdefault((name, pad, pin, PO.expected_mark(
                klass, marked, plus_sign), why), []).append(c.ref)
    for sym, refs in unnamed.items():
        out.append(Item("polarity", ln.mpn, refs, "",
                        f"symbol {sym} has no pin called K, so nothing says "
                        f"which end is the cathode; use a symbol that names it",
                        code="polarity.unnamed_cathode"))
    for (name, pad, pin, word, why), refs in wrong.items():
        out.append(Item("polarity", ln.mpn, refs, "",
                        f"{name} marks pad {pad} ({why}); that mark belongs "
                        f"at the {word}, which is pin {pin} of the symbol",
                        code="polarity.wrong_pad"))
    for name, refs in unmarked.items():
        out.append(Item("polarity", ln.mpn, refs, "",
                        f"polar part on {name}, which has no polarity mark",
                        code="polarity.polar_on_unmarked"))
    for (name, pad), refs in needless.items():
        out.append(Item("polarity", ln.mpn, refs, "",
                        f"bidirectional symbol on {name}, which marks pad {pad}; "
                        f"the assembler may ask which way round",
                        code="polarity.bidirectional_on_marked"))
    for name, refs in unsure.items():
        out.append(Item("footprint_mark", ln.mpn, refs, "", name))
    return out


def datasheet_items(ln, var):
    """What the datasheet says the marking means vs the symbol's class."""
    path = ln.table_path or ln.draft_path
    out = []
    for c in ln.comps:
        klass = PO.classify(c)
        if var.marked.startswith("other"):
            out.append(Item("polarity", ln.mpn, [c.ref], path,
                            f"datasheet: the mark is on '{var.marked[6:].strip()}'; "
                            f"bomcheck cannot tell which pin that is",
                            code="polarity.mark_unclear"))
        elif var.marked == "none" and klass in (PO.POLAR_DIODE, PO.POLAR_CAP):
            out.append(Item("polarity", ln.mpn, [c.ref], path,
                            f"datasheet: not polarized; {c.libsource} is a {klass}",
                            code="polarity.datasheet_not_polar"))
        elif var.marked != "none" and klass == PO.BIDIRECTIONAL:
            out.append(Item("polarity", ln.mpn, [c.ref], path,
                            f"datasheet: the marking is the {var.marked}; "
                            f"{c.libsource} is bidirectional",
                            code="polarity.datasheet_polar"))
    return out


def done_lines(lines, items):
    """Lines with no open item at all."""
    open_mpns = {i.mpn for i in items}
    return [ln for ln in lines
            if ln.table is not None and ln.mpn not in open_mpns]


class TableRow:
    """One pin table (one TOML file) and everything still open on it."""

    __slots__ = ("mpn", "refs", "path", "state", "listed", "pins", "missing",
                 "differ", "signed", "note")


def table_rows(lines, att):
    """-> [TableRow] for every line that has a table or a draft, done or not."""
    out = []
    for ln in lines:
        var, path = (ln.table, ln.table_path) if ln.table else (ln.draft, ln.draft_path)
        if var is None:
            continue
        pins = ln.comp.pins
        r = TableRow()
        r.mpn, r.refs, r.path = ln.mpn, ln.refs, path
        r.pins = len(pins)
        named = {n for n, v in var.pins.items() if var.numbered or v.strip()}
        if var.polarity:
            named = {p.num for p in pins} if var.marked else set()
        r.listed = len(named & {p.num for p in pins})
        r.missing = r.pins - r.listed
        found = P.compare(var, pins)
        bad = {f["pin"] for f in found if f["code"] in DISAGREE}
        r.differ = len(bad)
        r.signed = bool(P.Document(path, tomlite.load(path)).reviewed_by)
        half = PO.classify(ln.comp) and half_typed_pin_stub(ln)
        r.state = ("stub" if not named or half else
                   "draft" if ln.table is None else "table")
        if var.polarity or half:
            r.state += "*"
        broken, _why = P.structurally_disagrees(var, pins)
        r.note = ("unfinished answer; the wizard asks about the marking instead" if half else
                  "could not be read" if broken and named else "")
        out.append(r)
    return out
