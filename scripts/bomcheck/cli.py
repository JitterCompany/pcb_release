#!/usr/bin/env python3
"""bomcheck — validate BOM entries against manufacturer evidence.

Standalone: nothing here is referenced by pcb-release.sh or any workflow
yet. The generation half is interactive and touches the network, which does
not fit the existing check-job shape, so wiring is a later decision made on
evidence rather than in advance.

  bomcheck report  PROJECT_DIR [--only U1,U9] [--out FILE]
        What do we know, and what is wrong? Reads the netlist, resolves each
        pin-mapped part against the reference tables, and reports.

  bomcheck extract PROJECT_DIR --mpn MPN --pdf FILE [--pages 27-36]
                   [--pin-count N] [--package NAME]
        Propose a pin table from a datasheet and say exactly what it could not
        get. Writes a DRAFT reference file that stays failing until a human
        completes it -- a partial table must never read as a finished check.

  bomcheck suggest PROJECT_DIR [--only ...] [--out FILE]
        Propose the Manufacturer / MPN / datasheet-URL fields a symbol is
        missing, with the evidence for each. Writes nothing to the design.

  bomcheck review  PROJECT_DIR [--out FILE]
        Collect everything needing a human into ONE CSV: missing BOM fields,
        and pin names that differ from the datasheet. Open it in a spreadsheet;
        put anything in `apply` to accept a row, edit `value` to correct it.

  bomcheck apply   PROJECT_DIR [--dry-run] [--replace]
        Write the `_set` values into the .kicad_sch and record accepted pin
        renames as aliases. Rows that OVERWRITE an existing value need
        --replace; filling an empty field is nearly free to undo, overwriting
        an engineer's value is not.

  bomcheck promote PROJECT_DIR [--mpn MPN] [--force]
        Move a reviewed draft pin table into reference/datasheet/, where the
        checks read it. Refuses a draft that still has unexplained pin
        differences -- promoting is the act of saying it was reviewed.

  bomcheck stub    PROJECT_DIR [--only U1,Q1] [--all]
        Write a skeleton reference table for parts whose datasheet has no pin
        table to read -- the pin numbers filled in from the symbol, the names
        left blank for you to type from the drawing. Fifteen parts on this
        board have no other route.

  bomcheck fetch   PROJECT_DIR [--only ...] [--cache DIR]
        Resolve + download + verify datasheets. Never interactive, never fails
        the run: what could not be got becomes one batched line for a human.

Exit: 0 clean, 1 findings, 2 usage/unreadable input.
"""
import argparse
import glob
import re
import os
import subprocess
import symgraph as SYM
import library as LIB
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import atomic
import datasheet as D
import extract as X
import netlist as N
import ordercode as OC
import pintable as P
import tomlite
import decisions as DC
import schematic as SCH
import suggest as SG
import tables as T
import polarity as PO
import reffile as R
import todo as TD
import wizard as WZ


def find_project(proj):
    """-> (stem, schematic, attestations_dir). Mirrors the repo convention of
    deriving everything from the single *.kicad_pro."""
    pros = glob.glob(os.path.join(proj, "*.kicad_pro"))
    if len(pros) != 1:
        sys.exit(f"bomcheck: expected exactly one .kicad_pro in {proj}, found {len(pros)}")
    stem = os.path.basename(pros[0])[:-len(".kicad_pro")]
    sch = os.path.join(proj, stem + ".kicad_sch")
    if not os.path.isfile(sch):
        sys.exit(f"bomcheck: no schematic {sch}")
    att = os.path.abspath(os.path.join(proj, "..", "attestations"))
    return stem, sch, att


def export_netlist(sch, out_dir):
    """kicad-cli into a scratch dir. Never written into the project tree: a
    committed netlist is a stale-artifact hazard with no drift check."""
    out = os.path.join(out_dir, "netlist.xml")
    r = subprocess.run(["kicad-cli", "sch", "export", "netlist",
                        "--format", "kicadxml", "-o", out, sch],
                       capture_output=True)
    if r.returncode != 0 or not os.path.isfile(out):
        sys.exit(f"bomcheck: kicad-cli netlist export failed:\n"
                 f"{r.stderr.decode('utf-8', 'replace')[:800]}")
    return out


def in_scope(c):
    """Placed parts with a pin table worth checking, or a polarity to check."""
    return bool(c.libpart) and not c.not_placed and (
        c.libpart.pinmapped() or bool(PO.classify(c)))


def board_parts(xml, only=None):
    """-> [Comp] for the pin-mapped, placed parts we validate."""
    comps, _ = N.parse(xml)
    out = [c for c in comps
           if in_scope(c)]
    if only:
        want = {r.strip().upper() for r in only.split(",") if r.strip()}
        out = [c for c in out if c.ref.upper() in want]
    return out


def short_refs(refs, keep=4):
    return " ".join(refs) if len(refs) <= keep else \
        " ".join(refs[:keep - 1]) + f" +{len(refs) - keep + 1} more"


# Sections that are a conflict in the design, not work still to do.
CONFLICT = {"waiver_invalid", "mixed_symbols", "polarity", "connections",
            "interface_difference"}


def paint(text, *codes):
    """ANSI style, only on a terminal and without NO_COLOR (no-color.org)."""
    if not codes or os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return text
    return f"\033[{';'.join(codes)}m{text}\033[0m"


BOLD, RED, YELLOW, GREEN = "1", "31", "33", "32"


def todo_label(r):
    """What is left on one pin table, in a word or two."""
    polar = r.state.endswith("*")
    if r.state.startswith("stub"):
        return "marking" if polar else "type pins"
    if r.differ:
        return "pins differ"
    if r.missing:
        return "pins missing"
    return "confirm" if polar else "check pins"


def cmd_status(a):
    """The to-do list. Offline, a few seconds; everything else hangs off it."""
    stem, sch, att = find_project(a.project)
    with tempfile.TemporaryDirectory(prefix="bomcheck_") as tmp:
        xml = a.netlist or export_netlist(sch, tmp)
        parts, rev = board_parts(xml, a.only), N.design_rev(xml)
    waived = []
    lines, items = TD.collect(parts, att, a.project, rev, waived)
    done = TD.done_lines(lines, items)
    rel = lambda p: os.path.relpath(p, os.path.dirname(att)) if p else ""
    library = LIB.component_library(a.project)

    def where(path):
        """A table's file, said as the reader would look for it."""
        for root in LIB.read_roots(a.project):
            if path.startswith(root + os.sep):
                return f"{os.path.basename(root)}: {os.path.relpath(path, root)}"
        return os.path.relpath(path, os.path.join(att, "reference"))

    print(paint(f"bomcheck: {stem}", BOLD))
    print(f"{len(lines)} part numbers ({len(parts)} placed), design rev "
          f"{rev or '(none)'}. "
          + paint(f"{len(done)} checked.", *((GREEN,) if done else ())))
    if waived:
        known = sum(1 for _i, w in waived if w.until)
        print(f"Findings you accepted: {len(waived) - known} for good, {known} "
              f"as known issue(s) of rev {rev} (attestations/waivers.toml).")
    by_kind = {}
    for it in items:
        by_kind.setdefault(it.kind, []).append(it)

    def heading(text, colour):
        print("\n" + paint(text, BOLD, colour))

    def section(kind, head):
        its = by_kind.get(kind, [])
        if its:
            heading(f"{head} ({len(its)}):", RED if kind in CONFLICT else YELLOW)
            for it in its:
                print(f"  {it.mpn or '-':24s} {short_refs(it.refs):22s} {it.detail}".rstrip())

    section("not_prepared", "Not looked at yet, run `bomcheck prepare`")
    section("waiver_invalid", "Errors in waivers.toml")
    section("waiver_expired", "Known issues of an earlier revision")
    section("waiver_stale", "Accepted findings that no longer occur")
    section("mpn_missing", "Set the MPN field")
    section("mixed_symbols", "One part number placed with different symbols")
    section("polarity", "Polarity: datasheet, symbol and footprint disagree")
    section("footprint_mark", "Confirm which pad a footprint's polarity mark is at")
    section("datasheet_missing", "No datasheet in the schematic")
    section("connections", "Inside the part: datasheet and symbol disagree")
    section("connections_unread", "Compare the symbol's drawing with the datasheet")
    section("connections_unfilled", "Say which pins are connected inside the part "
            "(numbered-only datasheet)")
    section("interface_unset", "Pin names nothing checks: the datasheet only "
            "numbers the pins")
    section("interface_unfilled", "Interface tables to type from the spec")
    section("interface_difference", "Pin names that differ from the interface spec")
    section("interface_partial", "Pins the interface table does not list")
    section("interface_unsigned", "Interface tables nobody has checked yet")
    exs = TD.EX.load(att)
    exempt = {ln.mpn: exs[ln.mpn] for ln in lines
              if ln.mpn in exs and not exs[ln.mpn].problem}
    if exempt:
        print(f"\nNo datasheet on purpose ({len(exempt)}): "
              + ", ".join(f"{m} ({e.reason})" for m, e in sorted(exempt.items())))
    for it in by_kind.get("apply_pending", []):
        heading("Answers not yet written to the schematic:", YELLOW)
        print(f"  {it.detail}: run `bomcheck apply`")

    # One row per TOML file, so a file with several problems appears once.
    rows = [r for r in TD.table_rows(lines, att)
            if r.missing or r.differ or not r.signed or r.note]
    if rows:
        ref_dir = rel(os.path.join(att, "reference"))
        heading(f"Pins to check against the datasheet ({len(rows)}), files in "
                f"{ref_dir}/:", YELLOW)
        print("  pins = found in the datasheet / on the symbol")
        print(paint(f"  {'MPN':24s} {'refs':18s} {'to do':12s} {'pins':>7s} "
                    f"{'differ':>6s}  file", BOLD))
        order = ["marking", "type pins", "pins differ", "pins missing",
                 "check pins", "confirm"]
        for r, todo in sorted(((r, todo_label(r)) for r in rows),
                              key=lambda rt: (order.index(rt[1]), rt[0].mpn)):
            sub = where(r.path)
            label = paint(f"{todo:12s}", RED if todo == "pins differ" else YELLOW)
            differ = paint(f"{r.differ or '-':>6}", *((RED,) if r.differ else ()))
            print(f"  {r.mpn:24s} {short_refs(r.refs, 3):18s} {label} "
                  f"{r.listed:>3d}/{r.pins:<3d} {differ}  {sub}"
                  + (f"  ({r.note})" if r.note else ""))

    new = LIB.uncommitted(library) if library else []
    if new:
        heading(f"Signed tables not committed in {os.path.basename(library)} yet "
                f"({len(new)}):", YELLOW)
        for n in new:
            print(f"  {n}")
        print("  Commit them in that repository (a PR), so other boards get them.")

    if not items:
        print("\n" + paint("Nothing open. Every line is checked against a "
                           "signed table.", BOLD, GREEN))
        return 0
    nxt = ("bomcheck prepare" if "not_prepared" in by_kind else
           "bomcheck apply" if "apply_pending" in by_kind else "bomcheck wizard")
    print(f"\nNext: {nxt}")
    return 1


def cmd_wizard(a):
    """Walk the open items, asking only what needs a person."""
    stem, sch, att = find_project(a.project)
    with tempfile.TemporaryDirectory(prefix="bomcheck_") as tmp:
        xml = a.netlist or export_netlist(sch, tmp)
        parts, rev = board_parts(xml, a.only), N.design_rev(xml)
    lines, items = TD.collect(parts, att, a.project, rev)
    if any(i.kind in TD.AUTOMATIC for i in items):
        print("Some parts have not been looked at yet. Run `bomcheck prepare` "
              "first, so there is something to review.")
        return 1
    ui = None
    if a.gui:
        import webui
        ui = webui.WebUI(project=os.path.basename(os.path.abspath(a.project)))
    wiz = WZ.Wizard(att, a.project, lines, items,
                    cache=a.cache or D.default_cache(),
                    auto_open=False if a.no_open else None, rev=rev,
                    by=a.by or "", ui=ui)
    wiz.recollect = lambda: TD.collect(parts, att, a.project, rev)
    wiz.save_to = a.save_to
    if ui is None:
        return wiz.run()
    return webui.serve(wiz, ui, port=a.port, open_browser=not a.no_browser)


def cmd_add_pdf(a):
    """For documents behind a login: file a PDF you downloaded under the URL
    the schematic holds, so every lookup of that URL finds it."""
    key = a.url if a.url.startswith("http") else D.local_key(a.url)
    err = D.add_local_copy(key, a.pdf, a.cache or D.default_cache())
    if err:
        sys.exit(f"bomcheck: {err}")
    print(f"[bomcheck] {key} now resolves to a copy of {a.pdf}")
    return 0


def cmd_prune(a):
    """Remove waivers that match no finding any more, and list them."""
    import waivers as WV
    if a.only:
        sys.exit("bomcheck prune: not with --only; every waiver for the other "
                 "parts would look unused and be removed")
    stem, sch, att = find_project(a.project)
    with tempfile.TemporaryDirectory(prefix="bomcheck_") as tmp:
        xml = a.netlist or export_netlist(sch, tmp)
        parts, rev = board_parts(xml, a.only), N.design_rev(xml)
    _lines, items = TD.collect(parts, att, a.project, rev)
    stale = [i.findings[0] for i in items if i.kind == "waiver_stale"]
    for w in WV.prune(att, stale):
        print(f"[bomcheck] removed: {w.label()}  ({w.note})")
    print(f"[bomcheck] {len(stale)} waiver(s) removed from {WV.path_of(att)}")
    return 0


def cmd_report(a):
    stem, sch, att = find_project(a.project)
    with tempfile.TemporaryDirectory(prefix="bomcheck_") as tmp:
        xml = a.netlist or export_netlist(sch, tmp)
        parts = board_parts(xml, a.only)
        # Units as the wizard sees them: Q1A/Q1B of a dual part. Without them
        # S1 vs S reads as a difference here and as a convention there.
        drawings = SYM.load(a.project)
        for c in parts:
            units = drawings[c.libsource].unit_of() if c.libsource in drawings else {}
            for p in c.pins:
                p.unit = units.get(p.num, "")

        docs = [d for _f, d in TD.signed_docs(att, a.project)]
        interfaces = TD.interface_docs(att, a.project)
        exemptions = TD.EX.load(att)

        L, n_err, n_warn = [], 0, 0
        L.append(f"# BOM validation report — {stem}")
        L.append("")
        L.append(f"{len(parts)} pin-mapped part(s) examined.")
        L.append("")

        for c in sorted(parts, key=lambda x: x.ref):
            L.append(f"## {c.ref} — {c.value}")
            L.append("")
            L.append(f"- symbol `{c.libsource}` ({len(c.pins)} pins, "
                     f"pinmap `{N.pinmap_hash(c.pins)}`)")
            L.append(f"- footprint `{c.footprint}`")
            mpn_eff, mpn_src = c.mpn_effective
            L.append(f"- MPN `{mpn_eff}`"
                     + (" _(from the Value field — the MPN field is empty)_"
                        if mpn_src == "value-field" else "")
                     if mpn_eff else "- MPN **missing**")

            # The SCHEMATIC is the source of truth for datasheet URLs. A
            # pin-mapped part without one is a gap in the design data, not just
            # an inconvenience for this tool: the URL is what lets anyone --
            # human or script -- get back to the document the pinout was taken
            # from. Reported as a warning, and it is repo-internal, so it can
            # gate without anyone's website being involved.
            ds_state, _d = TD.datasheet_state(c, a.project, exemptions)
            if ds_state == "missing":
                L.append("- **WARNING** `datasheet_missing` — no URL, project "
                         "file or embedded file, and no exemption. `bomcheck "
                         "wizard` offers all four.")
                n_warn += 1

            if not mpn_eff:
                L.append("")
                L.append("**ERROR** `mpn_missing` — a pin-mapped part with no MPN "
                         "is neither validatable nor orderable.")
                L.append("")
                n_err += 1
                continue

            # --- axis 2: datasheet pin table -----------------------------
            var, err = P.find_variant(docs, mpn_eff, c.footprint)
            if err:
                L.append("")
                L.append(f"**ERROR** `variant_ambiguous` — {err}")
                n_err += 1
            elif var is None:
                L.append("")
                L.append("**no reference pin table** — nothing to check the symbol "
                         "against. This is the work item, not a defect.")
                n_warn += 1
            else:
                L.append(f"- reference `{var.doc}` / {var.part or '?'} / "
                         f"{var.package} ({var.source})")
                broken, why = P.structurally_disagrees(var, c.pins)
                if broken:
                    L.append("")
                    L.append(f"**ERROR** `reference_not_this_package` — {why}")
                    n_err += 1
                else:
                    findings = P.compare(var, c.pins)
                    # A DISAGREEMENT is a pin both sides describe differently.
                    # A pin the table simply does not cover is a gap in the
                    # EVIDENCE -- the STM32 table reads 96 of 100 pins -- and
                    # counting that as a defect would report a hundred errors
                    # for a part whose every read pin agrees.
                    bad = [f for f in findings
                           if f["code"] in ("pin_name_mismatch", "pin_polarity",
                                            "pin_unnamed")]
                    gaps = [f for f in findings
                            if f["code"] in ("pin_extra", "pin_missing",
                                             "reference_incomplete")]
                    notes = [f for f in findings
                             if f["code"] in ("pin_name_convention",
                                              "pin_name_partial")]
                    L.append("")
                    if bad:
                        L.append(f"**ERROR** `pin_table_mismatch` — "
                                 f"{len(bad)} pin(s) disagree with the datasheet:")
                        L.append("")
                        for f in bad:
                            L.append(f"  - `{f['code']}` {f['msg']}")
                        n_err += 1
                    else:
                        covered = len(set(var.pins) & {p.num for p in c.pins})
                        L.append(f"**OK** — all {covered} pin(s) the datasheet "
                                 f"covers agree with the symbol.")
                    doc = next(d for d in docs if var in d.variants)
                    if not doc.reviewed_by:
                        L.append("")
                        L.append("**ERROR** `reference_unsigned` — nobody has "
                                 "signed this table, so it is not evidence yet.")
                        n_err += 1
                    if gaps:
                        L.append("")
                        L.append(f"_{len(gaps)} pin(s) not covered by the "
                                 f"extracted table — evidence is incomplete, "
                                 f"not contradicted._")
                        n_warn += 1
                    for f in notes:
                        L.append(f"  - _{f['msg']}_")

            e, w = interface_report(L, c, var, interfaces)
            n_err, n_warn = n_err + e, n_warn + w
            L.append("")

        L.append("---")
        L.append("")
        L.append(f"**{n_err} error(s), {n_warn} warning(s)** over {len(parts)} parts.")
        text = "\n".join(L) + "\n"

    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"[bomcheck] wrote {a.out}")
    else:
        print(text)
    print(f"[bomcheck] report: {n_err} error(s), {n_warn} warning(s)")
    return 1 if n_err else 0


def interface_report(L, c, var, interfaces):
    """The symbol's names against its interface table, as report lines.
    -> (errors, warnings)."""
    name = TD.interface_of(c)
    numbered = (var is not None and var.numbered and len(c.pins) > 2
                and not TD.numbered_only(c.pins))
    if not name:
        if not numbered:
            return 0, 0
        L.append("")
        L.append(f"**ERROR** `interface_unset` — the datasheet only numbers the "
                 f"pins, so nothing checks the symbol's names. Add the symbol "
                 f"field `{TD.FIELD}` and sign the interface's table.")
        return 1, 0
    path, doc, iv = TD.find_interface(interfaces, name, c.libsource)
    L.append(f"- interface `{name}`" + (f", spec {doc.spec}" if doc else ""))
    L.append("")
    if iv is None:
        L.append(f"**ERROR** `interface_unfilled` — no signed {name} table "
                 f"covers `{c.libsource}`, so nothing checks its pin names.")
        return 1, 0
    errors, warnings = 0, 0
    broken, why = P.structurally_disagrees(iv, c.pins)
    found = [] if broken else P.compare(iv, c.pins)
    bad = [f for f in found if f["code"] in TD.DISAGREE]
    gaps = [f for f in found if f["code"] in TD.GAPS + ("reference_unfilled",)]
    if broken:
        L.append(f"**ERROR** `interface_not_this_symbol` — {why}")
        errors += 1
    elif bad:
        L.append(f"**ERROR** `interface_mismatch` — {len(bad)} pin(s) disagree "
                 f"with the {name} spec:")
        L.append("")
        for f in bad:
            L.append(f"  - `{f['code']}` {f['msg'].replace('datasheet', 'spec')}")
        errors += 1
    else:
        covered = len(set(iv.pins) & {p.num for p in c.pins})
        L.append(f"**OK** — all {covered} pin(s) the {name} spec covers agree "
                 f"with the symbol.")
    if not doc.reviewed_by:
        L.append("")
        L.append("**ERROR** `interface_unsigned` — nobody has signed this "
                 "interface table, so it is not evidence yet.")
        errors += 1
    if gaps:
        L.append("")
        L.append(f"_{len(gaps)} pin(s) not covered by the {name} table._")
        warnings += 1
    return errors, warnings


def parse_pages(spec):
    """'27-36,40' -> [27..36, 40]"""
    out = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def cmd_extract(a):
    if not a.mpn or not a.pdf:
        sys.exit("bomcheck extract: --mpn and --pdf are both required")
    stem, sch, att = find_project(a.project)

    text, pages = D.pdf_to_text(a.pdf)
    if not text.strip():
        sys.exit(f"bomcheck: no text in {a.pdf} (scanned PDF? try OCR)")

    v = D.verify(a.mpn, text, a.manufacturer or "")
    print(f"[bomcheck] {a.pdf}: {pages} pages, provenance {v['verdict']} — {v['why']}")
    if v["verdict"] == "wrong-part":
        sys.exit("bomcheck: refusing to extract a pin table from a datasheet that "
                 "does not describe this part")

    # ONE pipeline for every datasheet: scope to this part's table, apply the
    # known shapes strongest-first, then validate. No per-document branching.
    fam = a.family or (re.match(r"^[A-Z]+[0-9]+[A-Z]?[0-9]*", a.mpn.upper())
                       or re.match(r"^.{0,8}", a.mpn.upper())).group(0)
    pkg = a.package or ""
    if not pkg:
        target, oc_findings = OC.check_package(a.mpn, text, a.footprint or "",
                                               a.symbol_pins or 0)
        for f in oc_findings:
            print(f"[bomcheck] {f['code']}: {f['msg']}")
        if target:
            pkg = target
            print(f"[bomcheck] package derived from the order code: {pkg}")

    # STAGE ONE takes the MPN and the datasheet. Nothing from the symbol:
    # --symbol-pins is used only for the ordering cross-check above and for
    # stage two, never to steer extraction.
    expect, src = a.pin_count, "caller"
    if not expect and pkg:
        m = re.search(r"(\d{2,3})$", pkg)
        expect, src = (int(m.group(1)), "order code") if m else (0, "")
    fp_count = T.Context.package_pin_count(a.footprint or "")
    if not expect:
        doc_count, why = OC.document_pin_count(a.mpn, text, a.footprint or "")
        if doc_count:
            expect, src = doc_count, why
            if fp_count and fp_count != doc_count:
                print(f"[bomcheck] pin_count_mismatch: the datasheet gives "
                      f"{doc_count} pins ({why}) but the footprint name says "
                      f"{fp_count}")
    if not expect and fp_count:
        expect, src = fp_count, "footprint package size"
    if not expect:
        expect, why = OC.drawing_pin_count(text)
        src = why if expect else ""
    ctx = T.Context(mpn=a.mpn, family=fam, package=pkg,
                    footprint=a.footprint or "",
                    expect_pins=expect, expect_pins_source=src)
    if expect:
        pad = (f" plus {ctx.exposed_pads} exposed pad(s) from the footprint"
               if ctx.exposed_pads else "")
        print(f"[bomcheck] expecting {expect} pins (from the {src}){pad}")
    else:
        print("[bomcheck] pin count unknown — completeness cannot be checked")
    res = T.read_pin_table(text, ctx)

    if res["scope"]:
        print(f"[bomcheck] scoped to the table headed {res['scope']!r}")
    if res["by_shape"]:
        print("[bomcheck] shapes: "
              + ", ".join(f"{k}={v}" for k, v in res["by_shape"].items()))
    for n in res["notes"]:
        print(f"[bomcheck]   {n}")
    if res["overextracted"]:
        sys.exit(f"bomcheck: refusing a table of {res['overextracted']} pins for "
                 f"a {ctx.expect_pins}-pin part")
    if res["low_confidence"]:
        print(f"[bomcheck] {len(res['low_confidence'])} name(s) reassembled from "
              f"wrapped cells — REVIEW FIRST: "
              f"{', '.join(sorted(res['low_confidence'], key=lambda x: (len(x), x)))}")
    for pin, alts in sorted(res["conflicts"].items()):
        print(f"[bomcheck] conflict pin {pin}: {res['pins'].get(pin)!r} vs "
              + ", ".join(f"{n!r} ({sh})" for sh, n in alts))

    a._wrapped = set(res["low_confidence"])
    a._declared = (ctx.total_pins, ctx.expect_pins_source + (
        f", plus {ctx.exposed_pads} exposed pad(s)" if ctx.exposed_pads else ""))
    a._bus_role = X.spi_role(text)
    r = {"pins": res["pins"], "conflicts": {}, "pages_used": [],
         "coverage": {"declared": ctx.total_pins, "found": len(res["pins"]),
                      "missing": res["missing"],
                      "missing_ranges": res["missing_ranges"]}}
    a._att = att
    return finish_extract(a, v, r)


def _unused_legacy_path(a, text, v):
    want = parse_pages(a.pages)
    if not want:
        sugg = X.page_scores(text.split("\f"))[:8]
        if sugg:
            print("[bomcheck] no --pages given; candidate pin-table pages "
                  "(page, distinct pins): " + ", ".join(f"{p}({n})" for p, n in sugg))
    r = X.extract(text, want or None, a.pin_count or 0)
    return finish_extract(a, v, r)


def finish_extract(a, v, r):
    cov = r["coverage"]

    order = sorted(r["pins"], key=lambda x: (0, int(x), "") if str(x).isdigit()
                   else (1, 0, str(x)))
    print(f"[bomcheck] extracted {cov['found']} pin(s) from pages "
          f"{X.compress([str(i) for i in r['pages_used']]) or 'auto'}")
    for pin in order:
        print(f"    {pin:>4s}  {r['pins'][pin]}")

    if r["conflicts"]:
        print(f"\n[bomcheck] {len(r['conflicts'])} pin(s) got conflicting names — "
              f"resolve by hand:")
        for pin, vs in sorted(r["conflicts"].items()):
            opts = ", ".join(f"{n} ({how} p{pg})" for n, how, pg in vs)
            print(f"    pin {pin}: {opts}")

    missing = cov["missing_ranges"]
    if missing:
        print(f"\n[bomcheck] NOT extracted: pins {', '.join(missing)}")
        print("[bomcheck] fill these in by hand — one sitting, once ever.")

    draft = pintable_draft(a, v, r)
    out = a.out or os.path.join(a._att, "reference", "draft",
                                f"DRAFT-{safe(a.mpn)}.toml")
    atomic.write_text(out, draft)
    print(f"\n[bomcheck] wrote {out}")
    a._result = {"found": cov["found"], "expected": cov["declared"],
                 "missing": cov["missing_ranges"],
                 "conflicts": len(r["conflicts"]), "path": out}
    if missing or r["conflicts"]:
        print("[bomcheck] this draft is INCOMPLETE and will keep reporting "
              "`reference_incomplete` until the gaps are filled — by design.")
        return 1
    return 0


def safe(s):
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(s))


def pintable_draft(a, v, r):
    """A draft reference file that is honest about being a draft."""
    doc = (v["doc_ids"][0] if v["doc_ids"] else "UNKNOWN-DOC")
    order = sorted(r["pins"], key=lambda x: (0, int(x), "") if str(x).isdigit()
                   else (1, 0, str(x)))
    L = [f"# DRAFT — machine-proposed from {os.path.basename(a.pdf)}.",
         "# Review every line against the datasheet before promoting this.",
         "#",
         f"# provenance: {v['verdict']} — {v['why']}",
         ""]
    if r["coverage"]["missing_ranges"]:
        L += ["# NOT EXTRACTED — fill these in by hand, then remove this block:",
              "#   pins " + ", ".join(r["coverage"]["missing_ranges"]),
              ""]
    for pin, vs in sorted(r["conflicts"].items()):
        L.append(f"# CONFLICT pin {pin}: "
                 + ", ".join(f"{n} ({how} p{pg})" for n, how, pg in vs))
    if r["conflicts"]:
        L.append("")
    L += [f'doc_id       = "{doc}"',
          f'manufacturer = "{a.manufacturer or ""}"',
          'origin       = "datasheet"',
          'extracted_by = "bomcheck-extract"',
          "",
          "  [[variant]]",
          f'  package      = "{a.package or ""}"',
          f'  mpns         = ["{a.mpn}"]',
          f'  source       = "pages {a.pages or "auto"}"',
          '  confidence   = "draft"']
    declared, src = getattr(a, "_declared", (0, ""))
    if a.pin_count:
        L.append(f"  pin_count    = {a.pin_count}"
                 f"   # declared separately, so an incomplete table is detected")
    elif declared:
        L.append(f"  pin_count    = {declared}   # from the {src}, never the "
                 f"symbol")
    role, why = getattr(a, "_bus_role", ("", ""))
    if role:
        L.append(f'  bus_role     = "{role}"   # read from the datasheet: '
                 f'{why[:70]}')
    L += ["", "    [variant.pins]"]
    wrapped = getattr(a, "_wrapped", set())
    for pin in order:
        mark = ("   # REASSEMBLED from a wrapped cell — verify against the PDF"
                if pin in wrapped else "")
        L.append(f'    {pin} = "{r["pins"][pin]}"{mark}')
    L.append("")
    return "\n".join(L)


def resolve_verified(mpn, c, local, cache):
    url, text, verdict, _path = resolve_verified_doc(mpn, c, local, cache)
    return url, text, verdict


def resolve_verified_doc(mpn, c, local, cache):
    """-> (url, text, verdict) for the best VERIFIED datasheet, else ("","","").

    Local files first (name-similarity ordered), then the symbol's own URL, then
    vendor path patterns. Only confirmed/family-only results are returned: an
    unverified document must never become a suggestion.
    """
    src = D.field_source(c.field("Datasheet"), os.path.dirname(local))
    cands = list(D.resolve(mpn, symbol_url=src, local_dir=local))
    cands += D.candidate_urls(mpn, c.field("Manufacturer"))
    for origin, src in cands:
        f = D.fetch(mpn, origin, src, cache)
        if not f.ok:
            continue
        v = D.verify(mpn, f.text, c.field("Manufacturer"))
        if v["verdict"] in ("confirmed", "family-only"):
            url = src if str(src).startswith("http") else ""
            return url, f.text, v["verdict"], f.path
    return "", "", "", ""


def cmd_suggest(a):
    stem, sch, att = find_project(a.project)
    local = os.path.join(a.project, "datasheets")
    with tempfile.TemporaryDirectory(prefix="bomcheck_") as tmp:
        xml = a.netlist or export_netlist(sch, tmp)
        comps, _ = N.parse(xml)
    cache = a.cache or D.default_cache()

    want = None
    if a.only:
        want = {r.strip().upper() for r in a.only.split(",") if r.strip()}

    rows, seen = [], {}
    for c in comps:
        if not in_scope(c):
            continue
        if want and c.ref.upper() not in want:
            continue
        mpn, _src = c.mpn_effective
        missing = (not c.mpn or not c.field("Manufacturer")
                   or not str(c.field("Datasheet")).startswith("http"))
        if not missing:
            continue
        # One lookup per part number, not per placement.
        if mpn and mpn in seen:
            url, text, verdict = seen[mpn]
        else:
            url, text, verdict = (resolve_verified(mpn, c, local, cache)
                                  if mpn else ("", "", ""))
            if mpn:
                seen[mpn] = (url, text, verdict)
        sugg = SG.for_component(c, url, text, verdict)
        if sugg:
            rows.append((c.ref, mpn, sugg))

    text_out = SG.render(sorted(rows, key=lambda r: r[0]))
    out = a.out or os.path.join(att, "review", "bom-field-suggestions.md")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(text_out)
    n_ready = sum(1 for _, _, ss in rows for s in ss if s.confidence != "none")
    n_human = sum(1 for _, _, ss in rows for s in ss if s.confidence == "none")
    print(f"[bomcheck] {len(rows)} symbol(s) with missing BOM fields: "
          f"{n_ready} suggestion(s) ready to accept, {n_human} need a human")
    print(f"[bomcheck] wrote {out}")
    return 0


# Pin-name differences that look like a rename rather than a defect: the symbol
# names the net or the board's role for a pin. Judged only on shape -- a shared
# component, or one name contained in the other -- never on plausibility.
def _looks_like_rename(sym, ds):
    norm = lambda x: re.sub(r"[^A-Z0-9]", "", str(x).upper())
    a, b = norm(sym), norm(ds)
    if not a or not b:
        return False, ""
    if a in b or b in a:
        return True, "one name contains the other"
    pa = {norm(x) for x in re.split(r"[-/()\s]+", sym) if x}
    pb = {norm(x) for x in re.split(r"[-/()\s]+", ds) if x}
    shared = (pa & pb) - {""}
    if shared:
        return True, f"both name {', '.join(sorted(shared))}"
    return False, ""


def _load_refs(att):
    """Promoted references first, then drafts -- so a reviewed table wins."""
    import glob
    out = []
    for sub in ("datasheet", "draft"):
        d = os.path.join(att, "reference", sub)
        for f in sorted(glob.glob(os.path.join(d, "*.toml"))):
            try:
                out.append((f, P.Document(f, tomlite.load(f))))
            except Exception as e:                               # noqa: BLE001
                print(f"[bomcheck] cannot read {os.path.basename(f)}: {e}")
    return out


def cmd_prepare(a):
    """Everything that needs no human: get and verify datasheets, read pin
    tables into drafts, stub what cannot be read, propose BOM fields. Safe to
    re-run; it only works on lines that have nothing yet."""
    import contextlib
    import io
    import json
    import types

    stem, sch, att = find_project(a.project)
    local = os.path.join(a.project, "datasheets")
    cache = a.cache or D.default_cache()
    with tempfile.TemporaryDirectory(prefix="bomcheck_") as tmp:
        xml = a.netlist or export_netlist(sch, tmp)
        parts = board_parts(xml, a.only)
        a.netlist = a.netlist or xml
        lines, _items = TD.collect(parts, att, a.project)
        logdir = os.path.join(att, "review", "prepare-log")
        os.makedirs(logdir, exist_ok=True)
        results = []
        # --refresh: re-read machine-made tables nobody signed, with the
        # current extractor. Signed tables and typed stubs are never touched,
        # and names someone already accepted carry over to the new draft.
        kept_alias, old_paths = {}, {}
        if a.refresh:
            for ln in lines:
                path, var = ((ln.table_path, ln.table) if ln.table
                             else (ln.draft_path, ln.draft))
                if var is None or ln.mpn.startswith("("):
                    continue
                doc = P.Document(path, tomlite.load(path))
                if doc.reviewed_by or doc.origin == "manual":
                    continue
                kept_alias[ln.mpn] = dict(var.alias)
                old_paths[ln.mpn] = path
                ln.table = ln.draft = None
            print(f"[bomcheck] --refresh: {len(kept_alias)} unsigned machine-read "
                  f"table(s) will be read again")
        # Parts where only polarity can be known get a polarity stub, never a
        # pin table; a blank pin stub from before is replaced.
        for ln in lines:
            if ln.mpn.startswith("(") or not PO.classify(ln.comp):
                continue
            var = ln.table or ln.draft
            if var is None or (not var.polarity and
                               not any(v.strip() for v in var.pins.values())):
                old = ln.table_path or ln.draft_path
                url, text, verdict, _pdf = resolve_verified_doc(
                    ln.mpn, ln.comp, local, cache)
                path = R.write_polarity_stub(att, ln, text)
                _drop_old(old, path)
                print(f"  {ln.mpn:26s} {short_refs(ln.refs):22s} "
                      f"{verdict or 'no datasheet':13s} -> polarity stub")
                ln.draft, ln.draft_path = True, path
        todo = [ln for ln in lines if not ln.mpn.startswith("(")
                and ln.table is None and ln.draft is None]
        print(f"[bomcheck] {len(todo)} line(s) without a pin table")
        for ln in todo:
            c = ln.comp
            rec = {"mpn": ln.mpn, "refs": ln.refs, "symbol": c.libsource,
                   "footprint": c.footprint, "symbol_pins": len(c.pins)}
            url, text, verdict, pdf = resolve_verified_doc(ln.mpn, c, local, cache)
            rec.update(datasheet=url or ("(local file)" if text else ""),
                       verdict=verdict)
            outcome = "stub"
            if text:
                ns = types.SimpleNamespace(
                    project=a.project, mpn=ln.mpn, pdf=pdf,
                    pages=None, pin_count=0, package="", family="",
                    footprint=c.footprint, symbol_pins=len(c.pins),
                    manufacturer=c.field("Manufacturer"), out=None,
                    _att=att, _result=None)
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    try:
                        cmd_extract(ns)
                    except SystemExit as e:
                        print(f"[bomcheck] extract stopped: {e}")
                    except Exception as e:                       # noqa: BLE001
                        print(f"[bomcheck] extract crashed: {type(e).__name__}: {e}")
                with open(os.path.join(logdir, safe(ln.mpn) + ".txt"), "w",
                          encoding="utf-8") as f:
                    f.write(buf.getvalue())
                res = ns._result
                rec["extract"] = res
                if res and res["found"]:
                    outcome = "draft"
                    new = P.Document(res["path"], tomlite.load(res["path"]))
                    have = {n: nm for v in new.variants for n, nm in v.pins.items()}
                    for pin, name in kept_alias.get(ln.mpn, {}).items():
                        if pin in have:
                            DC.add_alias(res["path"], ln.mpn, pin, name, have[pin])
                elif res:
                    os.remove(res["path"])
            rec["outcome"] = outcome
            # The old table goes only once its replacement is on disk, so an
            # interrupted refresh never leaves a line with nothing.
            if outcome == "draft":
                _drop_old(old_paths.get(ln.mpn), res["path"])
            if outcome == "stub":
                # stub skips a part that still has a table, so the old read
                # goes first; a blank stub replaces it straight after.
                _drop_old(old_paths.get(ln.mpn), "")
                stub_ns = types.SimpleNamespace(
                    project=a.project, netlist=a.netlist, only=",".join(ln.refs),
                    force=False, cache=cache)
                with contextlib.redirect_stdout(io.StringIO()):
                    cmd_stub(stub_ns)
            print(f"  {ln.mpn:26s} {short_refs(ln.refs):22s} "
                  f"{verdict or 'no datasheet':13s} -> {outcome}"
                  + (f" ({rec['extract']['found']} pins read)"
                     if outcome == "draft" else ""))
            results.append(rec)

        # Kept across runs: one record per MPN, so the parsing history of a
        # part survives a later run that skips it.
        jpath = os.path.join(att, "review", "prepare.json")
        old = {}
        if os.path.isfile(jpath):
            old = {r["mpn"]: r for r in json.load(open(jpath, encoding="utf-8"))}
        old.update({r["mpn"]: r for r in results})
        atomic.write_text(jpath, json.dumps(sorted(old.values(),
                                                   key=lambda r: r["mpn"]), indent=1))

        print("[bomcheck] BOM fields:")
        a.out = None
        cmd_review(a)
    print("\nNext: bomcheck   (shows what is left for you)")
    return 0


def cmd_review(a):
    stem, sch, att = find_project(a.project)
    local = os.path.join(a.project, "datasheets")
    cache = a.cache or D.default_cache()
    with tempfile.TemporaryDirectory(prefix="bomcheck_") as tmp:
        xml = a.netlist or export_netlist(sch, tmp)
        comps, _ = N.parse(xml)

    want = ({r.strip().upper() for r in a.only.split(",") if r.strip()}
            if a.only else None)
    live = [c for c in comps
            if in_scope(c)
            and (not want or c.ref.upper() in want)]

    # One row per PART, not per placement: the fields are a property of the part
    # number, so filling them in once should cover all sixteen of its diodes.
    # Parts with no MPN are keyed by designator -- there is nothing else.
    groups = {}
    for c in live:
        mpn, _ = c.mpn_effective
        groups.setdefault(mpn or f"({c.ref})", []).append(c)

    rows, seen = [], {}
    for part, cs in sorted(groups.items()):
        mpn, _ = cs[0].mpn_effective
        currents, notes, prefill = {}, [], {}
        for _key, name in DC.FIELD_KEYS:
            vals = [(c.field(name) or "").strip() for c in cs]
            filled = sorted({v for v in vals if v})
            if len(filled) > 1:
                # Two different values for one part number: a genuine conflict.
                # Nothing is proposed, because picking one would be a guess
                # about which placement is right.
                currents[name] = DC._MIXED
                notes.append(f"{name} has {len(filled)} DIFFERENT values across "
                             f"{', '.join(sorted(x.ref for x in cs))} -- resolve "
                             f"by hand")
            elif filled and len(filled) < len(vals) and vals.count(filled[0]) < len(vals):
                # Some placements carry it and the rest are empty. That is not a
                # conflict, it is an incomplete fill -- the same part number
                # cannot have two datasheets -- so the one value that exists is
                # proposed for the placements missing it.
                currents[name] = filled[0]
                prefill[name] = filled[0]
                have = sum(1 for v in vals if v)
                notes.append(f"{have} of {len(vals)} placements carry this "
                             f"{name}; the rest are empty")
            else:
                currents[name] = filled[0] if filled else ""

        if mpn and mpn in seen:
            url, text, verdict = seen[mpn]
        else:
            url, text, verdict = (resolve_verified(mpn, cs[0], local, cache)
                                  if mpn else ("", "", ""))
            if mpn:
                seen[mpn] = (url, text, verdict)

        proposals = dict(prefill)
        for sg in SG.for_component(cs[0], url, text, verdict):
            if sg.confidence != "none" and sg.value:
                proposals[sg.field] = sg.value
                notes.append(sg.why)

        # A datasheet field that points at the WRONG document is a REPLACE, not
        # a gap -- and the more valuable finding. This board links TXU0204PWR to
        # txu0304.pdf, whose pin table belongs to a different part.
        cur_url = currents.get("Datasheet", "")
        if mpn and cur_url.startswith("http") and cur_url != DC._MIXED:
            f = D.fetch(mpn, "symbol-field", cur_url, cache)
            bad = (f.error if not f.ok else
                   (D.verify(mpn, f.text, cs[0].field("Manufacturer"))
                    if f.ok else None))
            if f.ok and bad and bad["verdict"] == "wrong-part":
                if url and url != cur_url:
                    proposals["Datasheet"] = url
                notes.append(f"the linked datasheet is not about {mpn}: "
                             f"{bad['why'][:90]}")
            elif not f.ok:
                notes.append(f"the linked datasheet could not be fetched: "
                             f"{f.error[:60]}")

        if not proposals and not any(v == DC._MIXED for v in currents.values()):
            continue
        # A prefill matches what some placements already say, so it is an `add`
        # for the empty ones rather than a REPLACE of anybody's value.
        shown = dict(currents)
        for name in prefill:
            shown[name] = ""
        rows.append(DC.field_row(part, sorted({c.ref for c in cs}), shown,
                                 proposals, "; ".join(dict.fromkeys(notes))[:300]))

    # --- pin differences ---------------------------------------------------
    refs = _load_refs(att)
    by_mpn = {}
    for c in live:
        m, _ = c.mpn_effective
        if m:
            by_mpn.setdefault(m, []).append(c)
    pin_rows, checked = [], 0
    for _f, doc in refs:
        for var in doc.variants:
            for mpn in var.mpns:
                for c in by_mpn.get(mpn, [])[:1]:
                    checked += 1
                    for f in P.compare(var, c.pins):
                        code = f["code"]
                        if code == "pin_name_convention":
                            # A recognised naming convention, not a difference.
                            # Listed anyway, marked accepted, so the rule is
                            # auditable -- a rule that silently equates names
                            # is one nobody can disagree with.
                            pin_rows.append(DC.pin_row(
                                mpn, c.ref, f["pin"], f["symbol"],
                                f["datasheet"], "convention (auto)", f["msg"],
                                accept="auto"))
                            continue
                        if code not in ("pin_name_mismatch", "pin_polarity"):
                            continue
                        rename, why = _looks_like_rename(f["symbol"],
                                                         f["datasheet"])
                        pin_rows.append(DC.pin_row(
                            mpn, c.ref, f["pin"], f["symbol"], f["datasheet"],
                            "likely rename" if rename else "needs judgement",
                            why or f["msg"]))

    base = a.out or os.path.join(att, "review")
    fpath = (base if base.endswith(".csv")
             else os.path.join(base, "decisions-fields.csv"))
    ppath = os.path.join(os.path.dirname(fpath), "decisions-pins.csv")
    # Keep whatever the reviewer already decided. Without this, re-running to
    # pick up a symbol fix quietly discards every mark made since last time.
    rows, c1 = DC.carry_over(fpath, rows, ["part"],
                             [f"{k}_set" for k, _ in DC.FIELD_KEYS])
    # Answers given in the wizard or typed into the CSV that nothing proposes
    # any more, but that the schematic does not hold yet, must survive too.
    rows += _unapplied_rows(fpath, rows, comps)
    pin_rows, c2 = DC.carry_over(ppath, pin_rows, ["part", "ref", "pin"],
                                 ["accept"])
    DC.write(fpath, rows, DC.FIELD_COLS)
    DC.write(ppath, pin_rows, DC.PIN_COLS)
    if c1 + c2:
        print(f"[bomcheck] carried over {c1 + c2} decision(s) you had already "
              f"made")

    DC.write_howto(os.path.join(os.path.dirname(fpath), "HOW-TO-EDIT.md"))
    n_set = sum(1 for r in rows for k, _ in DC.FIELD_KEYS if r.get(f"{k}_set"))
    n_rep = sum(1 for r in rows if r["action"] == "REPLACE")
    print(f"[bomcheck] {len(live)} placed part(s) -> {len(rows)} row(s) needing "
          f"attention; {n_set} field value(s) proposed, {n_rep} of them "
          f"REPLACING an existing value")
    n_auto = sum(1 for r in pin_rows if r["verdict"].startswith("convention"))
    print(f"[bomcheck] {checked} part(s) had a pin table (signed or draft); "
          f"{len(pin_rows) - n_auto} pin difference(s), "
          f"{n_auto} recognised naming convention(s) already accepted")
    print(f"[bomcheck] wrote {fpath}")
    print(f"[bomcheck]       {ppath}")
    print("[bomcheck] open in a spreadsheet. A `_set` cell with a value will be "
          "written; clear it to reject.")
    print("[bomcheck] then: bomcheck apply   (add --replace to allow REPLACE "
          "rows)")
    return 0


def _drop_old(old, new):
    if old and os.path.abspath(old) != os.path.abspath(new or "") \
            and os.path.isfile(old):
        os.remove(old)


def _unapplied_rows(path, rows, comps):
    """Old CSV rows with a `_set` value the schematic does not hold yet."""
    import csv
    if not os.path.isfile(path):
        return []
    have = {r["part"] for r in rows}
    by_ref = {c.ref: c for c in comps}
    keep = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if DC._unforce(r.get("part", "")) in have:
                continue
            refs = (r.get("refs") or "").split()
            for key, name in DC.FIELD_KEYS:
                val = DC._unforce(r.get(f"{key}_set", ""))
                if val and any(ref in by_ref and by_ref[ref].field(name) != val
                               for ref in refs):
                    keep.append(r)
                    break
    return keep


def cmd_stub(a):
    """Some datasheets simply have no pin table: a dual FET's pinout is a
    package drawing, a connector's is a mechanical outline. Those parts can only
    ever be validated from a table somebody types once.

    This writes the skeleton so that typing is the ONLY work: pin numbers from
    the symbol, names blank. The numbers come from the symbol because they are
    not in dispute -- a 6-pin SOT-363 has pins 1..6 whoever makes it -- while
    every NAME is left empty, because a name copied from the symbol would make
    the reference agree with the thing it exists to check.
    """
    stem, sch, att = find_project(a.project)
    local_ds = os.path.join(a.project, "datasheets")
    cache_ds = a.cache or D.default_cache()
    with tempfile.TemporaryDirectory(prefix="bomcheck_") as tmp:
        comps, _ = N.parse(a.netlist or export_netlist(sch, tmp))
    docs = _load_refs(att)
    covered = {m for _f, d in docs for v in d.variants for m in v.mpns}

    want = ({r.strip().upper() for r in a.only.split(",") if r.strip()}
            if a.only else None)
    groups = {}
    for c in comps:
        if not in_scope(c):
            continue
        if want and c.ref.upper() not in want:
            continue
        mpn, _ = c.mpn_effective
        if not mpn or (mpn in covered and not a.force):
            continue
        groups.setdefault(mpn, []).append(c)

    out_dir = os.path.join(att, "reference", "draft")
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for mpn, cs in sorted(groups.items()):
        c = cs[0]
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", mpn)
        path = os.path.join(out_dir, f"STUB-{safe}.toml")
        if os.path.isfile(path) and not a.force:
            continue
        refs = ", ".join(sorted({x.ref for x in cs}))
        pkg = c.footprint.partition(":")[2] or c.footprint

        # Quote any label rows from the package drawing. They save the
        # transcription; they do NOT give the numbering, because a text dump
        # cannot tell which row of a SOT-363 drawing was the bottom one, and
        # SOT-363 numbers from the bottom-left. Assigning numbers is the part a
        # human has to do, so it is the part left undone.
        hints = []
        _url, _text, _verdict = resolve_verified(mpn, c, local_ds, cache_ds)
        if _text:
            hints = X.drawing_labels(_text, len(c.pins))
        L = [f"# STUB — {mpn} has no pin table in any datasheet we could read.",
             f"# Type each name from the package drawing, then delete this",
             f"# header. Until every name is filled in, this stays a draft and",
             f"# nothing checks against it.",
             "#",
             f"# placed as: {refs}",
             f"# footprint: {c.footprint}",
             f"# datasheet: {c.field('Datasheet') or '(none recorded)'}",
             "#",
             "# The pin NUMBERS are from the symbol -- they are not in dispute.",
             "# The NAMES are blank on purpose: a name copied from the symbol",
             "# would make this reference agree with the thing it exists to",
             "# check.",
             "",
             f'doc_id       = ""   # e.g. "Diodes DS30567 Rev 8"',
             f'manufacturer = {tomlite.quote(c.field("Manufacturer"))}',
             'origin       = "manual"',
             'extracted_by = ""   # your name -- a person typed this',
             "",
             "  [[variant]]",
             f'  part         = {tomlite.quote(mpn)}',
             f'  package      = {tomlite.quote(pkg)}',
             f'  mpns         = [{tomlite.quote(mpn)}]',
             f'  source       = ""   # where in the document, e.g. "Figure 2"',
             '  confidence   = "manual"',
             f"  pin_count    = {len(c.pins)}",
             "",
             "    [variant.pins]"]
        if hints:
            L += ["# LABEL ROWS FOUND IN THE PACKAGE DRAWING. These are the",
                  "# names, saving you the transcription -- but NOT the",
                  "# numbering: a text dump cannot tell which row of the",
                  "# drawing was the bottom one, and this package numbers",
                  "# from the bottom-left. Assign them from the figure.",
                  "#"]
            for _line, _toks in hints[:4]:
                L.append(f"#     {_line}")
            L.append("")
        for pin in sorted((p.num for p in c.pins),
                          key=lambda x: (0, int(x), "") if x.isdigit()
                          else (1, 0, x)):
            L.append(f'    {pin} = ""')
        L.append("")
        atomic.write_text(path, "\n".join(L))
        written.append((mpn, len(c.pins), refs))

    for mpn, n, refs in written:
        print(f"[bomcheck] wrote STUB-{re.sub(r'[^A-Za-z0-9._-]', '_', mpn)}.toml"
              f"  ({n} pins, {refs})")
    print(f"[bomcheck] {len(written)} stub(s) in {out_dir}")
    if written:
        print("[bomcheck] fill in the names, then: bomcheck promote")
    return 0


def cmd_promote(a):
    """A draft is what the extractor proposed; a promoted file is what a human
    agreed to, and only promoted files are read by the checks. Promotion is
    therefore the moment a machine-made table becomes evidence -- so it is
    refused while the table still disagrees with the board in ways nobody has
    explained."""
    stem, sch, att = find_project(a.project)
    draft_dir = os.path.join(att, "reference", "draft")
    live_dir = os.path.join(att, "reference", "datasheet")
    with tempfile.TemporaryDirectory(prefix="bomcheck_") as tmp:
        comps, _ = N.parse(a.netlist or export_netlist(sch, tmp))
    by_mpn = {}
    for c in comps:
        if in_scope(c):
            m, _ = c.mpn_effective
            if m:
                by_mpn.setdefault(m, []).append(c)

    import glob
    import shutil
    moved, blocked = [], []
    for f in sorted(glob.glob(os.path.join(draft_dir, "*.toml"))):
        doc = P.Document(f, tomlite.load(f))
        mpns = [m for v in doc.variants for m in v.mpns]
        if a.mpn and a.mpn not in mpns:
            continue
        unresolved = []
        for v in doc.variants:
            for m in v.mpns:
                for c in by_mpn.get(m, [])[:1]:
                    broken, why = P.structurally_disagrees(v, c.pins)
                    if broken:
                        unresolved.append(f"STRUCTURE: {why}")
                        continue
                    # reference_unfilled and reference_incomplete belong here
                    # too: a stub nobody has typed in, or a table covering half
                    # the part, is not something to promote into evidence.
                    unresolved += [x["msg"] for x in P.compare(v, c.pins)
                                   if x["code"] in ("pin_name_mismatch",
                                                    "pin_polarity",
                                                    "reference_unfilled",
                                                    "reference_incomplete")]
        if not doc.reviewed_by:
            unresolved.insert(0, "not signed: add reviewed_by and reviewed_on, "
                                 "or use `bomcheck wizard`")
        name = os.path.basename(f)
        if unresolved and not a.force:
            blocked.append((name, unresolved))
            continue
        to = live_dir
        library = LIB.component_library(a.project)
        if library:
            first = next((c for m in mpns for c in by_mpn.get(m, [])), None)
            scope = (a.save_to if a.save_to in ("library", "project") else
                     LIB.scope_of(first.libsource, a.project) if first else "project")
            to = LIB.signed_dir(library) if scope == "library" else live_dir
        dst = os.path.join(to, re.sub(r"^(DRAFT|STUB)-", "", name))
        os.makedirs(to, exist_ok=True)
        if not a.dry_run:
            shutil.move(f, dst)
        moved.append((name, os.path.relpath(dst, os.path.dirname(att)), len(unresolved)))

    for src, dst, n in moved:
        note = f" (FORCED over {n} difference(s))" if n else ""
        print(f"[bomcheck] {'would promote' if a.dry_run else 'promoted'} "
              f"{src} -> {dst}{note}")
    for name, un in blocked:
        print(f"::warning::[bomcheck] {name}: not ready ({len(un)} problem(s)); "
              f"`bomcheck wizard` walks through them")
        for u in un[:3]:
            print(f"[bomcheck]     {u}")
    print(f"[bomcheck] {len(moved)} promoted, {len(blocked)} still under review")
    return 1 if blocked and not a.force else 0


def cmd_apply(a):
    stem, sch, att = find_project(a.project)
    base = os.path.dirname(a.decisions) if a.decisions else os.path.join(att, "review")
    fpath = a.decisions or os.path.join(base, "decisions-fields.csv")
    ppath = os.path.join(os.path.dirname(fpath), "decisions-pins.csv")
    if not os.path.isfile(fpath):
        sys.exit(f"bomcheck: no decisions at {fpath} -- run `bomcheck review`")

    field_edits = DC.parse_fields(fpath)
    all_pins = DC.parse_pins(ppath)
    pin_edits = [p for p in all_pins if p.accepted and not p.auto]
    n_auto = sum(1 for p in all_pins if p.auto)
    print(f"[bomcheck] {len(field_edits)} field value(s) to write, "
          f"{len(pin_edits)} pin difference(s) accepted"
          + (f" ({n_auto} already handled by a naming rule, nothing recorded)"
             if n_auto else ""))

    edits, missing = [], []
    for e in field_edits:
        if e.problem:
            missing.append(e.problem)
            continue
        if e.is_replace and not a.replace:
            missing.append(f"{'/'.join(e.refs)}.{e.field}: REPLACE "
                           f"{e.current!r} -> {e.value!r} skipped; pass "
                           f"--replace to overwrite an existing value")
            continue
        for ref in e.refs:
            inst = SCH.find(a.project, ref)
            if inst is None:
                missing.append(f"{ref}: not found in any sheet")
                continue
            edits.append((ref, e.field, e.value, inst))

    # One symbol block can serve several designators, so the same edit may be
    # queued once per designator. Collapse, or the verification reads back a
    # value written twice and the log claims more work than happened.
    seen, unique = set(), []
    for ref, field, value, inst in edits:
        key = (inst.path, inst.start, field)
        if key in seen:
            continue
        seen.add(key)
        unique.append((ref, field, value, inst))

    applied, problems = SCH.apply_edits(unique, dry_run=a.dry_run)
    for ref, field, value in applied:
        print(f"[bomcheck]   {'would set' if a.dry_run else 'set'} "
              f"{ref}.{field} = {value!r}")

    # Aliases, matched by the part the DESIGNATOR carries -- never by pin number
    # alone. Pin 22 exists in most tables, so a pin-only match filed U8's alias
    # under the ADS114S08 document, where it would have suppressed a real
    # difference on a different part.
    refs = _load_refs(att)
    with tempfile.TemporaryDirectory(prefix="bomcheck_") as tmp:
        comps, _ = N.parse(a.netlist or export_netlist(sch, tmp))
    mpn_of = {}
    for c in comps:
        m, _ = c.mpn_effective
        if m:
            mpn_of[c.ref] = m

    n_alias = 0
    for p in pin_edits:
        mpn = mpn_of.get(p.ref)
        if not mpn:
            missing.append(f"{p.ref}: no MPN, so no reference table to record "
                           f"the alias in")
            continue
        done = False
        for f, doc in refs:
            for var in doc.variants:
                if mpn in var.mpns and str(p.pin) in var.pins and not done:
                    if not a.dry_run:
                        DC.add_alias(f, mpn, p.pin, p.symbol, p.datasheet)
                    n_alias += 1
                    done = True
                    print(f"[bomcheck]   {'would record' if a.dry_run else 'recorded'}"
                          f" alias {p.symbol} == {p.datasheet} for {mpn} in "
                          f"{os.path.basename(f)}")
        if not done:
            missing.append(f"{p.ref} pin {p.pin}: no reference table for {mpn} "
                           f"holds that pin")

    for m in missing + problems:
        print(f"::warning::[bomcheck] {m}")
    print(f"[bomcheck] {len(applied)} field(s), {n_alias} alias(es)"
          + (" (dry run, nothing written)" if a.dry_run else ""))
    if applied and not a.dry_run:
        # KiCad keeps a copy of each symbol field on its footprint. Until the
        # layout is updated, DRC's schematic parity reports every changed field.
        print("[bomcheck] The layout still has the old values. In the PCB editor: "
              "Tools > Update PCB from Schematic (F8), then save the board.")
    return 1 if (problems or missing) else 0


# Best-first ranking of provenance verdicts. A failed fetch is a distinct
# outcome from a rejected one and is reported as such -- "st.com timed out" and
# "that PDF is the wrong part" call for different human actions.
_RANK = {"confirmed": 0, "family-only": 1, "unreadable": 2, "wrong-part": 3,
         "fetch-failed": 4}


def cmd_fetch(a):
    stem, sch, att = find_project(a.project)
    cache = a.cache or D.default_cache()
    local = os.path.join(a.project, "datasheets")
    with tempfile.TemporaryDirectory(prefix="bomcheck_") as tmp:
        xml = a.netlist or export_netlist(sch, tmp)
        parts = [c for c in board_parts(xml, a.only) if c.mpn]

    rows, todo, seen = [], [], set()
    for c in parts:
        if c.mpn in seen:
            continue
        seen.add(c.mpn)

        attempts = []
        for origin, src in D.resolve(c.mpn, symbol_url=D.field_source(
                                         c.field("Datasheet"), a.project),
                                     local_dir=local):
            label = f"{origin}:{os.path.basename(src)}"
            f = D.fetch(c.mpn, origin, src, cache)
            if not f.ok:
                attempts.append({"label": label, "verdict": "fetch-failed",
                                 "why": f.error, "f": None})
                continue
            v = D.verify(c.mpn, f.text, c.field("Manufacturer"))
            attempts.append({"label": label, "verdict": v["verdict"],
                             "why": v["why"], "f": f})
            if v["verdict"] == "confirmed":
                break

        if not attempts:
            rows.append((c.mpn, "-", "missing", "-", "no candidate source at all"))
            todo.append((c.mpn, "no datasheet URL, no archived PDF, no mirror -- "
                         f"drop a PDF in {os.path.relpath(local)}/", []))
            continue

        best = min(attempts, key=lambda x: _RANK.get(x["verdict"], 9))
        pages = str(best["f"].pages) if best["f"] else "-"
        # Naming a rejected file as the "best source" reads as an endorsement.
        # Nothing at wrong-part or worse is a source at all.
        usable = _RANK.get(best["verdict"], 9) <= 1
        rows.append((c.mpn, best["label"] if usable else "(none usable)",
                     best["verdict"], pages if usable else "-", best["why"][:64]))
        if best["verdict"] != "confirmed":
            # Show the whole trail: a human needs to see that the vendor site
            # failed AND that the archived PDF was the wrong part, because those
            # are two different problems with two different fixes.
            trail = [f"{x['label']}: {x['verdict']} -- {x['why'][:90]}"
                     for x in attempts]
            todo.append((c.mpn, f"no confirmed datasheet "
                         f"({len(attempts)} source(s) tried)", trail))

    print(f"{'MPN':26s} {'BEST SOURCE':44s} {'VERDICT':12s} {'PG':>4s}")
    for r in rows:
        print(f"{r[0]:26s} {r[1]:44s} {r[2]:12s} {r[3]:>4s}")
    ok = sum(1 for r in rows if r[2] == "confirmed")
    print(f"\n[bomcheck] {ok}/{len(rows)} datasheet(s) confirmed; cache: {cache}")
    if todo:
        print(f"\n[bomcheck] {len(todo)} item(s) need a human -- batched, do these "
              f"in one sitting:")
        for mpn, why, trail in todo:
            print(f"\n  {mpn}: {why}")
            for t in trail:
                print(f"      tried {t}")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, helptext in (("status", cmd_status, "what is left to do (default)"),
                               ("wizard", cmd_wizard, "answer the open items one by one"),
                               ("prune", cmd_prune, "remove waivers that match nothing any more"),
                               ("prepare", cmd_prepare, "the automatic half: datasheets, drafts, stubs"),
                               ("report", cmd_report, "validate and report"),
                               ("fetch", cmd_fetch, "resolve+verify datasheets"),
                               ("extract", cmd_extract, "propose a pin table"),
                               ("suggest", cmd_suggest, "propose missing BOM fields"),
                               ("review", cmd_review, "one file of decisions"),
                               ("apply", cmd_apply, "carry out ticked decisions"),
                               ("promote", cmd_promote, "draft -> checked"),
                               ("stub", cmd_stub, "skeleton for a hand-typed table")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("project", nargs="?", default=".",
                       help="KiCad project directory (default: here)")
        p.add_argument("--only", help="restrict to these refs, e.g. U1,U6,U9")
        p.add_argument("--netlist", help="use a pre-exported kicadxml netlist")
        p.add_argument("--out", help="write the report here instead of stdout")
        p.add_argument("--cache", help="datasheet cache directory")
        p.add_argument("--mpn", help="extract: the part this datasheet describes")
        p.add_argument("--pdf", help="extract: the datasheet PDF")
        p.add_argument("--pages", help="extract: page range, e.g. 27-36 (the "
                                       "chapter a human points at)")
        p.add_argument("--pin-count", type=int, default=0,
                       help="extract: how many pins the part HAS, per the package "
                            "drawing -- lets an incomplete table be detected")
        p.add_argument("--package", help="extract: package name, e.g. TSSOP-16")
        p.add_argument("--manufacturer", help="extract: manufacturer name")
        p.add_argument("--family", help="extract: device family stem for "
                       "multi-package tables, e.g. STM32L4R5")
        p.add_argument("--footprint", help="extract: the board footprint, "
                       "cross-checked against the package the MPN encodes")
        p.add_argument("--decisions", help="apply: the decisions file")
        p.add_argument("--refresh", action="store_true",
                       help="prepare: read unsigned machine-made tables again "
                            "with the current extractor")
        p.add_argument("--force", action="store_true",
                       help="promote: promote even with unexplained differences")
        p.add_argument("--replace", action="store_true",
                       help="apply: also carry out rows that OVERWRITE an "
                            "existing value, not just fill empty fields")
        p.add_argument("--dry-run", action="store_true",
                       help="apply: show what would change, write nothing")
        p.add_argument("--no-open", action="store_true",
                       help="wizard: do not open datasheets by themselves")
        p.add_argument("--gui", action="store_true",
                       help="wizard: ask in a browser tab, with the datasheet "
                            "and footprint beside the question")
        p.add_argument("--port", type=int, default=None,
                       help="wizard --gui: port on 127.0.0.1 (default 8790, or "
                            "any free one if taken; 0 = any free one)")
        p.add_argument("--no-browser", action="store_true",
                       help="wizard --gui: only print the address, open nothing")
        p.add_argument("--save-to", choices=["ask", "library", "project"], default="ask",
                       help="wizard, promote: where a signed table goes. ask (default "
                            "for the wizard) proposes the library for parts from a "
                            "component library or KiCad's own, the project otherwise")
        p.add_argument("--library", metavar="DIR", default="",
                       help="the component library repo to save shared tables in, "
                            "when the project's sym-lib-table names more than one "
                            "(also BOMCHECK_LIBRARY)")
        p.add_argument("--by", metavar="NAME",
                       help="wizard: your name, recorded with what you "
                            "check. Default: git user.name of the board's repo")
        p.add_argument("--symbol-pins", type=int, default=0,
                       help="extract: symbol pin count, cross-checked too")
        p.set_defaults(fn=fn)
    p = sub.add_parser("add-pdf", help="use a PDF you downloaded for a URL "
                                       "that needs a login")
    p.add_argument("url", metavar="URL_OR_MPN",
                   help="the URL in the Datasheet field; or the MPN, for a "
                        "datasheet that is embedded or kept out of the repo")
    p.add_argument("pdf", help="the PDF you downloaded from it")
    p.add_argument("--cache", help="datasheet cache directory")
    p.set_defaults(fn=cmd_add_pdf)
    argv = sys.argv[1:]
    if not argv or argv[0] not in sub.choices and not argv[0].startswith("-"):
        argv = ["status"] + argv
    a = ap.parse_args(argv)
    if getattr(a, "library", ""):   # read by every command that loads tables
        os.environ["BOMCHECK_LIBRARY"] = a.library
    try:
        return a.fn(a)
    finally:
        ignore_review(a.project)


def ignore_review(project):
    """review/ holds working files that bomcheck regenerates; once applied,
    their answers are in the schematic and the signed tables. A .gitignore
    inside it keeps the folder out of git without the board repo having to
    say so."""
    review = os.path.abspath(os.path.join(project or ".", "..", "attestations", "review"))
    marker = os.path.join(review, ".gitignore")
    if os.path.isdir(review) and not os.path.exists(marker):
        try:
            atomic.write_text(marker, "# bomcheck working files, regenerated\n*\n")
        except OSError:
            pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[bomcheck] stopped. Nothing is half-written; run the same "
              "command again to continue.", file=sys.stderr)
        sys.exit(130)
