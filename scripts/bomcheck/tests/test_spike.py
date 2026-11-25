#!/usr/bin/env python3
"""Phase-0 spike assertions — each one a pass/fail claim, not a judgement.

These encode what the spike set out to prove. The load-bearing pair is
tmux_broken vs tmux_fixed: the SAME MPN in a defective tree and a fixed one. A
check that passes both, or fails both, is worthless regardless of how good its
output looks.

Offline only. Network behaviour is exercised by `bomcheck fetch`, not here, so
this suite cannot go red because a vendor website changed.

Usage: python3 tests/test_spike.py [-v]        (exit 0 pass, 1 fail)
"""
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import csv
import datasheet as D
import netlist as N
import extract as X
import ordercode as OC
import pintable as P
import tomlite
import decisions as DC
import schematic as SCH
import suggest as SG
import tables as T

FIX = os.path.join(HERE, "fixtures")
# A few tests run against a real board's data, which is not public. Point
# BOMCHECK_TEST_BOARD at the folder that holds attestations/ and the board
# folder; without it those tests are skipped.
BOARD = os.environ.get("BOMCHECK_TEST_BOARD", "")
REF = (os.path.join(BOARD, "attestations", "reference", "datasheet") if BOARD
       else os.path.join(HERE, "fixtures", "reference"))
DS = (next(iter(glob.glob(os.path.join(BOARD, "*", "datasheets"))), "")
      if BOARD else "")

FAILS = []


def check(cond, name, detail=""):
    if cond:
        if "-v" in sys.argv:
            print(f"  ok   {name}")
    else:
        FAILS.append(f"{name}{': ' + detail if detail else ''}")


def one(fixture):
    comps, _ = N.parse(os.path.join(FIX, fixture))
    return comps[0]


def test_defect_is_caught():
    """The whole point: same MPN, one broken tree, one fixed tree."""
    docs = P.load_dir(REF)
    if not docs:
        FAILS.append("no reference tables found -- is the board clone present?")
        return

    broken = one("tmux_broken.xml")
    check(broken.libsource == "Jitter_IC:TMUX1109", "fixture: broken uses TMUX1109",
          broken.libsource)
    check(broken.mpn == "TMUX1309APWR", "fixture: broken MPN is TMUX1309APWR",
          broken.mpn)
    var, err = P.find_variant(docs, broken.mpn, broken.footprint)
    check(var is not None and not err, "broken: reference resolves", str(err))
    if var:
        f = P.compare(var, broken.pins)
        check(len(f) == 16, "broken: ALL 16 pins disagree with the datasheet",
              f"got {len(f)}")
        by = {x["pin"]: x for x in f}
        # The two pins the incident report singles out.
        check(by.get("8", {}).get("datasheet") == "GND"
              and by["8"]["symbol"] == "DA",
              "broken: pin 8 datasheet GND vs symbol DA")
        check(by.get("14", {}).get("datasheet") == "S1A"
              and by["14"]["symbol"] == "VDD",
              "broken: pin 14 datasheet S1A vs symbol VDD")

    fixed = one("tmux_fixed.xml")
    check(fixed.libsource == "Jitter_IC:TMUX1309A", "fixture: fixed uses TMUX1309A",
          fixed.libsource)
    check(fixed.mpn == broken.mpn, "fixture: SAME MPN in both trees")
    var, err = P.find_variant(docs, fixed.mpn, fixed.footprint)
    if var:
        f = P.compare(var, fixed.pins)
        check(not f, "fixed: PASSES cleanly (negative control)",
              "; ".join(x["msg"] for x in f))


def test_polarity_is_not_lost():
    """A datasheet prints "EN" in the NAME column and "Active low logic input"
    in the prose beside it; KiCad encodes that as ~{EN}. Transcribing only the
    name column discards polarity, and EN polarity was one of the real
    differences between TMUX1109 and TMUX1309.

    Built from a literal fixture rather than a file in the working data, so it
    cannot break when that data is promoted, demoted or regenerated."""
    v = P.Variant("d", {"package": "TSSOP-16", "mpns": ["TMUX1309APWR"],
                        "pins": {"6": "EN", "8": "GND"},
                        "active_low": ["6"]})
    check("6" in v.active_low, "polarity: the reference records pin 6 active-low")
    check(v.polarity_known, "polarity: ...so this reference can be compared on it")

    ok = [N.Pin("6", "~{EN}", "input"), N.Pin("8", "GND", "power_in")]
    check(not P.compare(v, ok), "polarity: a correct ~{EN} symbol agrees",
          str(P.compare(v, ok)))

    wrong = [N.Pin("6", "EN", "input"), N.Pin("8", "GND", "power_in")]
    got = [f["code"] for f in P.compare(v, wrong)]
    check(got == ["pin_polarity"], "polarity: an active-HIGH EN is caught", str(got))

    # And a reference that never established polarity must not claim one.
    v2 = P.Variant("d", {"package": "TSSOP-16", "mpns": ["X"],
                         "pins": {"6": "EN"}})
    check(not v2.polarity_known and not P.compare(v2, [N.Pin("6", "~{EN}", "input")]),
          "polarity: a table that did not read polarity asserts nothing about it")


def test_drawing_labels_give_names_not_numbering():
    """A datasheet with no pin table still prints the names in the package
    drawing, in rows:

        D2    G1      S1
        S2    G2      D1

    Quoting those saves the transcription. What is NOT recoverable is which row
    is pins 1..3 -- SOT-363 numbers from the bottom-left and a text dump has no
    idea which row was the bottom -- so both readings are self-consistent and
    inferring one would be a coin flip on a dual FET's gate and drain."""
    drawing = ("   D2    G1      S1\n"
               "   S2    G2      D1\n"
               "The device is AEC-Q101 qualified for high reliability.\n")
    rows = X.drawing_labels(drawing, 6)
    got = {tuple(t) for _l, t in rows}
    check(("D2", "G1", "S1") in got and ("S2", "G2", "D1") in got,
          "drawing: both label rows found", str(got))
    check(not any("qualified" in l for l, _t in rows),
          "drawing: prose is not mistaken for a label row", str(rows))

    # Single-spaced words are prose, not a drawing row.
    check(not X.drawing_labels("G1 G2 S1 D1 is a list\n", 6),
          "drawing: a single-spaced line is not a label row")

    # It returns LABELS, never a mapping -- nothing here assigns a number.
    for _line, toks in rows:
        check(all(isinstance(t, str) and not t.isdigit() for t in toks),
              "drawing: labels only, no pin numbers inferred")


def test_unfilled_stub_says_so():
    """Some datasheets have no pin table -- a dual FET's pinout is a package
    drawing -- so those parts can only be validated from a table somebody types.
    A stub is that skeleton: numbers from the symbol, names blank.

    Comparing an unfilled one yields a confusing mismatch per pin ("datasheet
    says ''"), when the honest statement is that nobody has typed it yet. And
    it must not promote: an empty table would otherwise become evidence."""
    v = P.Variant("d", {"package": "SOT-363", "mpns": ["M"],
                        "pins": {"1": "", "2": "", "3": ""}})
    sym = [N.Pin("1", "G1", "input"), N.Pin("2", "S1", "passive"),
           N.Pin("3", "D2", "passive")]
    fs = P.compare(v, sym)
    check([f["code"] for f in fs] == ["reference_unfilled"],
          "stub: an unfilled table reports itself, not three mismatches",
          str([f["code"] for f in fs]))
    check("3 of 3" in fs[0]["msg"], "stub: it says how much is missing",
          fs[0]["msg"])

    # A partly filled one is still unfilled.
    v2 = P.Variant("d", {"package": "SOT-363", "mpns": ["M"],
                         "pins": {"1": "G1", "2": "", "3": "D2"}})
    check(P.compare(v2, sym)[0]["code"] == "reference_unfilled",
          "stub: partly typed is still not typed")

    # Filled and correct -> clean.
    v3 = P.Variant("d", {"package": "SOT-363", "mpns": ["M"],
                         "pins": {"1": "G1", "2": "S1", "3": "D2"}})
    check(not P.compare(v3, sym), "stub: once typed in, it compares normally")


def test_no_connect_is_agreement():
    """A no-connect the symbol leaves blank is both sides saying the same
    thing. Treating it as a mismatch failed two otherwise-clean parts on a pin
    that carries no signal."""
    v = P.Variant("d", {"package": "P", "mpns": ["M"],
                        "pins": {"4": "N/C", "5": "VDD"}})
    sym = [N.Pin("4", "~", "no_connect"), N.Pin("5", "VDD", "power_in")]
    check(not P.compare(v, sym), "no-connect: blank symbol pin agrees with N/C",
          str(P.compare(v, sym)))

    # ...but a REAL name left blank is still a finding.
    v2 = P.Variant("d", {"package": "P", "mpns": ["M"], "pins": {"4": "VREF"}})
    got = [f["code"] for f in P.compare(v2, [N.Pin("4", "~", "passive")])]
    check(got == ["pin_unnamed"], "no-connect: a named pin left blank is caught",
          str(got))


def test_provenance_rejects_known_bad_pdfs():
    """Two wrong PDFs sat in a test board's datasheets/. Both must be
    rejected. Vendor PDFs are not in this repo: skipped without the board."""
    if not os.path.isdir(DS):
        print("skip: provenance of real PDFs (set BOMCHECK_TEST_BOARD)")
        return
    cases = [
        ("ADS114S08IPBSR", "ads114s08b.pdf", "wrong-part",
         "B-variant datasheet vs the fitted non-B part"),
        ("EG915NEAAC-N05-SNNSA", "quectel_ec21_series_hardware_design_v2-3-1.pdf",
         "wrong-part", "EC21 document next to an EG915N line"),
        ("BC847BPN", "BC847BPN.pdf", "confirmed", "control: the right part"),
        ("SIC472ED-T1-GE3", "vishay_sic47x_75786.pdf", "confirmed",
         "control: family datasheet naming the exact order code"),
    ]
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        for mpn, pdf, want, why in cases:
            path = os.path.join(DS, pdf)
            if not os.path.isfile(path):
                FAILS.append(f"missing fixture PDF {pdf}")
                continue
            f = D.fetch(mpn, "local-archive", path, tmp)
            v = D.verify(mpn, f.text, "")
            check(v["verdict"] == want, f"provenance: {mpn} vs {pdf} -> {want} ({why})",
                  f"got {v['verdict']}")


def test_whole_token_matching():
    """The substring trap: ADS114S08 IS inside ADS114S08B."""
    text = "ADS114S08B " * 60 + "Texas Instruments delta-sigma ADC " * 20
    check(D.verify("ADS114S08IPBSR", text, "")["verdict"] == "wrong-part",
          "whole-token: ADS114S08IPBSR is not confirmed by ADS114S08B text")
    check(D.verify("ADS114S08B", text, "")["verdict"] == "confirmed",
          "whole-token: ADS114S08B is confirmed by its own text")


def test_near_family_never_merges():
    """SMA6J13A (unidirectional) and SMA6J13CA (bidirectional) differ by one
    character. Nothing may treat them as the same part."""
    v = P.Variant("doc", {"package": "SMA", "mpns": ["SMA6J13A-TR"],
                          "pins": {"1": "K", "2": "A"}})
    check(v.covers("SMA6J13A-TR"), "enumeration matches the listed part")
    check(not v.covers("SMA6J13CA-TR"),
          "enumeration does NOT match the bidirectional neighbour")


def test_enumeration_xor_decoder():
    v = P.Variant("doc", {"package": "0603", "mpns": ["X"], "mpn_match": "y",
                          "pins": {}})
    try:
        v.covers("X")
        FAILS.append("mpns + mpn_match together must be rejected")
    except ValueError:
        check(True, "mpns and mpn_match are mutually exclusive")


def test_extract_matches_hand_transcription():
    """The extractor must reproduce the table a human read off the same page.

    Uses the cached TMUX text if present; skipped when it is absent so the suite
    stays offline-clean."""
    txt = os.path.join(HERE, "fixtures", "tmux1309a_p7.txt")
    if not os.path.isfile(txt):
        check(True, "extract: skipped, no cached datasheet text")
        return
    want = {"1": "S0B", "2": "S2B", "3": "DB", "4": "S3B", "5": "S1B",
            "6": "EN", "8": "GND", "9": "A1", "10": "A0", "11": "S3A",
            "12": "S0A", "13": "DA", "14": "S1A", "15": "S2A", "16": "VDD"}
    r = X.extract(open(txt, encoding="utf-8").read(), pages_wanted=[1],
                  pin_count=16)
    got = r["pins"]
    check(len(got) == 16, "extract: all 16 pins recovered", f"got {len(got)}")
    bad = {k: (want[k], got.get(k)) for k in want if got.get(k) != want[k]}
    check(not bad, "extract: every pin matches the hand transcription", str(bad))
    check(got.get("16") == "VDD",
          "extract: VDD is a pin name, not table furniture (regression)")


def test_extract_number_first_full_word_types():
    """TI's ADS114S08B writes the type column as a PHRASE ("Analog input",
    "Digital ground") and uses an em dash for its no-connect pin.

    Both broke extraction in ways that were invisible until measured: with only
    abbreviation types this table yielded ZERO of 32 pins, and a \\b boundary
    after the type column cannot follow an em dash, so pin 25 (NC) was dropped
    even after the phrase types were added."""
    txt = os.path.join(HERE, "fixtures", "ads114s08b_p4.txt")
    if not os.path.isfile(txt):
        check(True, "extract/ads: skipped, no cached text")
        return
    r = X.extract(open(txt, encoding="utf-8").read(), pages_wanted=[1],
                  pin_count=32)
    check(r["coverage"]["found"] == 32, "ads: all 32 pins recovered",
          f"got {r['coverage']['found']}, missing "
          f"{r['coverage']['missing_ranges']}")
    check(r["pins"].get("1") == "AINCOM", "ads: number-first row parsed")
    check(r["pins"].get("14") == "DGND", "ads: 'Digital ground' phrase type")
    check(r["pins"].get("25") == "NC", "ads: em-dash type column (no-connect)")
    check(r["pins"].get("32") == "REFP1/AIN6", "ads: slash in a pin name")


def test_multipackage_column_choice():
    """ST publishes ONE table for every package the die ships in, and the SAME
    package appears once per device family with different numbering.

    In DS12023, LQFP100 is column x=16 for STM32L4R5xxx/L4R7xxx and x=115 for
    STM32L4R9xxx. Measured against the board's own 100-pin symbol, the first
    agrees on 88 of 89 pins and the second on 15 of 80 — so a wrong column is
    not a near miss, it is a complete, confident, wrong pin table.

    The column must be chosen from the DOCUMENT. Choosing it by whichever column
    best fits the symbol would be circular: the symbol is the thing under test."""
    txt = os.path.join(HERE, "fixtures", "stm32_p83.txt")
    if not os.path.isfile(txt):
        check(True, "multipackage: skipped, no cached page")
        return
    page = open(txt, encoding="utf-8").read()

    cols = X._columns_from_headers(page)
    check(len(cols) == 14, "multipackage: 14 package columns found",
          f"got {len(cols)}")
    check(sum(1 for _, p in cols if p == "LQFP100") == 2,
          "multipackage: LQFP100 appears twice (once per device family)")

    groups = X._split_groups(cols)
    check(len(groups) == 2, "multipackage: split into 2 family groups",
          f"got {len(groups)}")
    check(len(groups[0]) == 8 and len(groups[1]) == 6,
          "multipackage: groups are 8 and 6 columns wide")

    x, why = X.choose_column(page, "STM32L4R5", "LQFP100")
    check(x == 16, "multipackage: picks the L4R5 LQFP100 column (x=16)",
          f"got {x}: {why}")
    x9, _ = X.choose_column(page, "STM32L4R9", "LQFP100")
    check(x9 == 115, "multipackage: picks the L4R9 LQFP100 column (x=115)",
          f"got {x9}")
    check(x != x9, "multipackage: the two families resolve to DIFFERENT columns")

    xn, why = X.choose_column(page, "STM32F7", "LQFP100")
    check(xn is None and "refusing to guess" in why,
          "multipackage: refuses to guess for an unknown family", why[:60])


def test_wrapped_name_recovery():
    """ST centres the pin-name cell on its row, so a long name is emitted as a
    stack of fragments straddling the number line, leaving that line's name
    column empty. Page 79 of DS12023 renders PC14-OSC32_IN(PC14) as
    "PC14-" / "OSC32" / <row> / "_IN" / "(PC14)".

    Two hazards are asserted, because both produced WRONG names before:
      * the previous row (pin 7, PC13) already has its own name and must not
        swallow "PC14-", which sits 2 lines from it and 8 from its true owner;
      * PF0 is absent from LQFP100 (its cell is "-") but its name sits 4 lines
        from pin 9 and must not drift onto it.
    """
    txt = os.path.join(HERE, "fixtures", "stm32_p79.txt")
    if not os.path.isfile(txt):
        check(True, "wrapped: skipped, no cached page")
        return
    page = open(txt, encoding="utf-8").read()
    got = X.recover_wrapped_names(page, 14, 16)
    check(got.get("8") == "PC14-OSC32_IN (PC14)",
          "wrapped: pin 8 reassembled in order", repr(got.get("8")))
    check(got.get("9") == "PC15-OSC32_OUT (PC15)",
          "wrapped: pin 9 reassembled in order", repr(got.get("9")))
    check("7" not in got,
          "wrapped: pin 7 already had its own name and absorbs nothing")
    check(all("PF0" not in v for v in got.values()),
          "wrapped: PF0 (absent from this package) does not drift onto a pin",
          str(got))


def test_order_code_decoding():
    """The MPN encodes the package; the datasheet publishes the decoding scheme.

    Neither is guessed: the scheme is parsed from the document, so no vendor
    tables are hardcoded (which would be wrong within a year, and silently).
    """
    txt = os.path.join(HERE, "fixtures", "stm32_ordering.txt")
    if not os.path.isfile(txt):
        check(True, "ordercode: skipped, no cached scheme")
        return
    text = open(txt, encoding="utf-8").read()
    scheme = OC.parse_scheme(text)
    check(len(scheme) >= 7, "ordercode: scheme parsed", f"got {len(scheme)}")
    names = [n for n, _ in scheme]
    check("pin count" in names and "package" in names,
          "ordercode: pin count and package sections present", str(names))

    d, left = OC.decode("STM32L4R5VIT6", scheme)
    check(left == "", "ordercode: whole order code consumed", repr(left))
    check(OC.package_of(d) == ("LQFP", 100),
          "ordercode: STM32L4R5VIT6 -> LQFP, 100 pins", str(OC.package_of(d)))

    # "I" is BOTH a flash-size code and a package code; only position separates
    # them, which is why sections are consumed in the document's own order.
    d2, _ = OC.decode("STM32L4R7AII6", scheme)
    check(OC.package_of(d2) == ("UFBGA", 169),
          "ordercode: repeated code 'I' disambiguated by position",
          str(OC.package_of(d2)))
    d3, _ = OC.decode("STM32L4R9ZIJ6P", scheme)
    check(OC.package_of(d3) == ("UFBGA", 144),
          "ordercode: trailing option suffix tolerated", str(OC.package_of(d3)))

    # A section that fails BEFORE anything matched must abort, or a late
    # section swallows the family prefix and "decodes" the part as option "S".
    bad, _ = OC.decode("QWERTY123", scheme)
    check(bad == [], "ordercode: a non-matching part decodes to nothing")

    target, f = OC.check_package("STM32L4R5VIT6", text,
                                 "Package_QFP:LQFP-100_14x14mm_P0.5mm", 100)
    check(target == "LQFP100" and not f,
          "ordercode: MPN, footprint and symbol all agree", str(f))
    _, f2 = OC.check_package("STM32L4R5VIT6", text,
                             "Package_QFP:LQFP-144_20x20mm_P0.5mm", 100)
    check(any(x["code"] == "pin_count_mismatch" for x in f2),
          "ordercode: a 100-pin part on a 144-pin land is caught", str(f2))
    _, f3 = OC.check_package("STM32L4R5VIT6", text,
                             "Package_QFP:LQFP-100_14x14mm_P0.5mm", 144)
    check(any(x["code"] == "symbol_pin_count_mismatch" for x in f3),
          "ordercode: a symbol with the wrong pin count is caught", str(f3))


def test_url_patterns_are_offered_not_trusted():
    """Guessing a datasheet URL is safe ONLY because verify() judges the result.

    A guess that lands on the wrong part is rejected exactly like a wrong
    archived PDF, so the failure mode is "no datasheet", never "the wrong
    datasheet". These assertions cover the shape of the guesses; whether a
    vendor's site is up is not this suite's business.
    """
    ti = D.candidate_urls("ADS114S08IPBSR", "Texas Instruments")
    check(any(u.endswith("/ads114s08.pdf") for _, u in ti),
          "urlpattern: TI stem shortened to the family document",
          str([u for _, u in ti][:3]))
    check(all(u.startswith("https://") for _, u in ti), "urlpattern: https only")

    we = D.candidate_urls("691322310003", "Wurth Elektronik")
    check(len(we) >= 2 and all("we-online.com" in u for _, u in we),
          "urlpattern: Wuerth offers both live paths", str(we))

    di = D.candidate_urls("DMN67D8LDW-7", "Diodes Incorporated")
    check(any(u.endswith("/DMN67D8LDW.pdf") for _, u in di),
          "urlpattern: Diodes packing suffix stripped", str(di))

    check(D.candidate_urls("U.FL-R-SMT-1(10)", "Hirose") == [],
          "urlpattern: no pattern is invented for an unknown vendor")


def test_three_column_table_is_not_skipped():
    """Microchip's MCP9701 has a THREE-column multi-package table (SC70 /
    SOT-23 / TO-92) under a two-line header ("Pin No." above the package name).

    A leftover threshold of four package columns -- reasonable for ST, wrong in
    general -- excluded the only page that mattered, leaving the reader working
    from a package-marking page whose columns do not line up. The table is
    perfectly well formed; the tool was looking past it.
    """
    txt = os.path.join(HERE, "fixtures", "mcp9701_p9.txt")
    if not os.path.isfile(txt):
        check(True, "3-col: skipped, no cached page")
        return
    page = open(txt, encoding="utf-8").read()
    cols = X._columns_from_headers(page)
    check([c[1] for c in cols] == ["SC70", "SOT-23", "TO-92"],
          "3-col: bare package names read as columns", str(cols))
    x, why = X.choose_column(page, "MCP9701",
                             "Package_TO_SOT_SMD:SOT-353_SC-70-5")
    check(x == 4, "3-col: the board footprint selects SC70, not SOT-23",
          f"{x}: {why}")
    pins, _ = X.extract_multicolumn(page, "MCP9701",
                                    "Package_TO_SOT_SMD:SOT-353_SC-70-5")
    check(pins == {"1": "NC", "2": "GND", "3": "VOUT", "4": "VDD", "5": "NC"},
          "3-col: all five pins, from the SC70 column", str(pins))


def test_last_section_does_not_run_to_eof():
    """The final scoped section must be bounded, or it swallows the rest of the
    document. On the TMUX datasheet that meant the ordering addendum, whose
    rows look enough like pins to yield 21 of them for a 16-pin part -- caught
    by the over-extraction guard, but the table was then lost entirely."""
    # A form feed is not a newline, so real pages carry a form feed then a newline, and the
    # heading must start a line for the anchor to see it.
    doc = ("Pin Functions: A\n  IN  1  I  x\n\f\n"
           "Pin Functions: B\n  IN  1  I  y\n\f\nP2\n\f\nP3\n\f\nP4\n")
    spans = X.section_spans(doc)
    check(len(spans) == 2, "section: both headings found", str(len(spans)))
    check(spans[-1][2] < len(doc),
          "section: the last span stops short of end-of-document",
          f"{spans[-1][2]} vs {len(doc)}")


def test_shape_name_then_columns():
    """The mirror of number_columns: NAME first, then one column per package.

        NAME          X2SON           SOT-23
        IN              4                1          I

    Choosing the X2SON column for a SOT-23 part gives a complete, confident,
    wrong table -- so the column comes from the header, matched against the
    board footprint."""
    txt = os.path.join(HERE, "fixtures", "tps7a20_sot23.txt")
    if not os.path.isfile(txt):
        check(True, "name_then_columns: skipped, no cached section")
        return
    text = open(txt, encoding="utf-8").read()
    ctx = T.Context(mpn="TPS7A2033PDBVR",
                    footprint="Package_TO_SOT_SMD:TSOT-23-5", expect_pins=5)
    r = T.shape_name_then_columns(text, ctx)
    check(r.pins == {"1": "IN", "2": "GND", "3": "EN", "4": "N/C", "5": "OUT"},
          "name_then_columns: reads the SOT-23 column", str(r.pins))

    other = T.Context(mpn="TPS7A2033YFPR", footprint="X2SON", expect_pins=5)
    r2 = T.shape_name_then_columns(text, other)
    check(r2.pins.get("4") == "IN",
          "name_then_columns: a different package gives a DIFFERENT mapping",
          str(r2.pins))


SCH_FIXTURE = """(kicad_sch
	(symbol
		(lib_id "Device:R")
		(property "Reference" "R1"
			(at 0 0 0)
			(effects (font (size 1.27 1.27)))
		)
		(property "Value" "10k"
			(at 0 0 0)
			(effects (font (size 1.27 1.27)))
		)
		(instances
			(project "x"
				(path "/aaa" (reference "R1") (unit 1))
				(path "/bbb" (reference "R9") (unit 1))
			)
		)
	)
)
"""


def test_schematic_property_write():
    """Edits are byte splices into the original text, so nothing outside the
    property can move or be reformatted -- a schematic holds fields this tool
    does not understand, and re-serialising would risk all of them."""
    insts = SCH._instances_of(SCH_FIXTURE)
    check(len(insts) == 1, "sch: one instance parsed", str(len(insts)))
    i = insts[0]
    check(i.prop("Value") == "10k", "sch: property read", str(i.prop("Value")))

    # existing property -> replaced in place
    out = SCH.set_property(SCH_FIXTURE, i, "Value", "22k")
    j = SCH._instances_of(out)[0]
    check(j.prop("Value") == "22k", "sch: existing property changed",
          str(j.prop("Value")))
    # Same-length replacement: the file must be identical apart from those
    # three bytes, which is the whole claim of a splice.
    check(len(out) == len(SCH_FIXTURE) and out.replace("22k", "10k") == SCH_FIXTURE,
          "sch: only the value bytes changed",
          f"delta {len(out) - len(SCH_FIXTURE)}")

    # absent property -> added
    out2 = SCH.set_property(SCH_FIXTURE, i, "MPN", "RC0603FR-0710KL")
    k = SCH._instances_of(out2)[0]
    check(k.prop("MPN") == "RC0603FR-0710KL", "sch: missing property added",
          str(k.prop("MPN")))
    check(k.prop("Value") == "10k", "sch: ...without disturbing the others")

    # quotes in a value must not break the file
    out3 = SCH.set_property(SCH_FIXTURE, i, "MPN", 'a"b\\c')
    check(SCH._instances_of(out3)[0].prop("MPN") == 'a"b\\c'
          and 'a\\"b\\\\c' in out3,
          "sch: quotes and backslashes are escaped in the file, read back as typed",
          repr(SCH._instances_of(out3)[0].prop("MPN")))


def test_symbol_serving_several_designators():
    """A sheet used more than once shares ONE symbol block: the Reference
    property is only the default and the real designators live in (instances).
    On this board a single connector is J5, J6, J7 AND J8 -- looking up J7 by
    the Reference property alone reports it as absent from a board it is on,
    and writing to it silently changes all four."""
    i = SCH._instances_of(SCH_FIXTURE)[0]
    check(i.refs == ["R1", "R9"], "sch: both designators found", str(i.refs))
    check("R9" in i.refs, "sch: the non-default designator is matched")


def test_differential_naming_convention():
    """KiCad detects a differential pair from a _N/_P SUFFIX, so a schematic
    that wants pairs recognised must move the polarity letter to the end:
    the datasheet's REFN0 becomes REF0_N. That is a convention forced by the
    tool, not a discrepancy.

    It tolerates a SPELLING, never a pin: base, polarity and index must all
    agree, so REFN0 vs REFN1 and REFN0 vs REFP0 stay different."""
    import re as _re
    parts = lambda s: [x for x in _re.split(r"[-/()\s]+", s) if x]

    def same(a, b):
        pa, pb = parts(a), parts(b)
        return len(pa) == len(pb) and all(
            P._norm_pin_name(x) == P._norm_pin_name(y) or P._same_differential(x, y)
            for x, y in zip(pa, pb))

    for a, b in (("REF0_N", "REFN0"), ("REF1_P", "REFP1"), ("VIN_N", "VINN"),
                 ("D_P", "DP"), ("REF1_N/AIN7", "REFN1/AIN7")):
        check(same(a, b), f"diff-naming: {a} == {b}")
    for a, b in (("REF0_N", "REFN1"), ("REF0_P", "REFN0"), ("AIN7", "AIN6"),
                 ("REF1_P/AIN6", "REFN1/AIN7")):
        check(not same(a, b), f"diff-naming: {a} != {b} -- a pin, not a spelling")

    # ...and it is reported, not silent: a rule nobody can see is a rule
    # nobody can disagree with.
    v = P.Variant("d", {"package": "P", "mpns": ["M"],
                        "pins": {"1": "REFN0", "2": "GND"}})
    fs = P.compare(v, [N.Pin("1", "REF0_N", "passive"),
                       N.Pin("2", "GND", "power_in")])
    check([f["code"] for f in fs] == ["pin_name_convention"],
          "diff-naming: reported as a convention, not a mismatch",
          str([f["code"] for f in fs]))


def test_spi_role_is_read_not_assumed():
    """What a serial pin is called from the BOARD's end depends on which end of
    the link the part is:

        slave :  DIN = MOSI     master:  DIN = MISO

    So equating DIN with MOSI unconditionally would bless a genuinely wrong
    symbol on any master. The role is READ from the datasheet -- a device whose
    chip-select and clock are INPUTS is being selected and clocked by somebody
    else -- and where the document does not say, the difference goes to a human.
    """
    slave = ("9   CS    Digital input    Chip select; active low\n"
             "11  SCLK  Digital input    Serial clock input\n")
    role, why = X.spi_role(slave)
    check(role == "peripheral", "spi-role: CS and SCLK as inputs mean peripheral", role)
    check("CS" in why, "spi-role: the evidence is quoted", why[:50])

    master = ("21  SCK   Digital output   Serial clock output\n"
              "22  NCS   Digital output   Chip select output\n")
    check(X.spi_role(master)[0] == "controller",
          "spi-role: driving them means controller")

    # An MCU can be either, and its datasheet describes both -- exactly the case
    # where a guess would be dangerous.
    both = slave + master
    check(X.spi_role(both)[0] == "",
          "spi-role: a part that is described both ways stays UNKNOWN")
    check(X.spi_role("no serial pins here")[0] == "",
          "spi-role: absent evidence is not evidence")

    # And the three outcomes in matching.
    sym = [N.Pin("1", "~{MOSI}", "input")]
    mk = lambda r: P.Variant("d", {"package": "P", "mpns": ["M"],
                                   "pins": {"1": "DIN"}, "bus_role": r})
    check([f["code"] for f in P.compare(mk("slave"), sym)]
          == ["pin_name_convention"],
          "spi-role: on an established slave, MOSI is DIN named from the board")
    check([f["code"] for f in P.compare(mk("master"), sym)]
          == ["pin_name_mismatch"],
          "spi-role: on an established MASTER the same naming is WRONG, and is "
          "reported as a mismatch rather than softened into a question",
          str([f["code"] for f in P.compare(mk("master"), sym)]))
    check([f["code"] for f in P.compare(mk(""), sym)]
          == ["pin_name_role_unknown"],
          "spi-role: unknown role asks rather than guesses")
    check(not [f for f in P.compare(mk("master"),
                                    [N.Pin("1", "MISO", "input")])
               if f["code"] != "pin_name_convention"],
          "spi-role: correct naming for a master is recognised too")


def test_conventions_live_in_matching_not_extraction():
    """Stage one records what the datasheet SAYS. If a naming convention leaked
    into extraction, the stored evidence would be a house style rather than the
    document, and the reference could no longer contradict the symbol."""
    import glob as _glob
    src = ""
    for f in ("tables.py", "extract.py", "ordercode.py"):
        path = os.path.join(os.path.dirname(HERE), f)
        if os.path.isfile(path):
            src += open(path, encoding="utf-8").read()
    # Reading a FACT from the document is stage one: "this part's chip-select
    # is an input, so it is a slave" is something the datasheet says. Applying
    # that fact to rename a pin -- DIN becomes MOSI -- is matching. So the
    # equating functions must be absent from extraction, while spi_role() may
    # live there and does.
    for token in ("_same_differential", "_diff_form", "polarity moved",
                  "_spi_equivalent", "_spi_candidate"):
        check(token not in src,
              f"layering: extraction does not equate names via {token!r}")
    check("def spi_role" in src,
          "layering: ...but reading the role FROM the document belongs there")

    # And the stored table keeps the datasheet's own spelling.
    d = os.path.join(BOARD, "attestations", "reference", "draft")
    f = os.path.join(d, "DRAFT-ADS114S08IPBSR.toml")
    if os.path.isfile(f):
        text = open(f, encoding="utf-8").read()
        check('"REFN0"' in text and '"REF0_N"' not in text,
              "layering: the draft stores the datasheet spelling, verbatim")


def test_parameter_rows_are_not_pins():
    """Electrical-characteristics tables are full of "TA = +25°C" and
    "PW = 10s", and with a column of test-condition numbers to their left they
    have exactly the shape of a pin row. Diodes' DMP6023 has NO pin table -- it
    shows a package drawing -- and this invented pin 2 "TA", pin 4 "PW" and pin
    6 "DC" from its ratings tables."""
    spec = ("  2      TA  = +25°C          Ambient temperature\n"
            "  4      PW  = 10s            Pulse width\n"
            "  6      DC  = 1%             Duty cycle\n"
            "  8      ID  = -5A            Drain current\n")
    ctx = T.Context(mpn="DMP6023LFG-7", expect_pins=8)
    check(not T.shape_number_name_only(spec, ctx).pins,
          "parameter rows: a ratings table yields no pins",
          str(T.shape_number_name_only(spec, ctx).pins))

    # A real pin table in the same shape still reads.
    real = ("Pin    Symbol    Description\n"
            "  1      E1     emitter TR1\n  2      B1     base TR1\n"
            "  3      C2     collector TR2\n  4      E2     emitter TR2\n")
    got = T.shape_number_name_only(real, T.Context(mpn="BC857BS", expect_pins=6))
    check(got.pins.get("1") == "E1" and len(got.pins) == 4,
          "parameter rows: a real number+name table still reads", str(got.pins))


def test_sim_holder_contact_numbers():
    """A SIM holder's drawing names its contacts C1..C8 (ISO 7816), the
    symbol names the signals. The standard mapping matches them, and a swap
    is still caught."""
    import tempfile
    ref = os.path.join(tempfile.mkdtemp(), "R.toml")
    open(ref, "w", encoding="utf-8").write(
        'doc_id = "X"\n\n  [[variant]]\n  package = "P"\n  mpns = ["M"]\n'
        '\n    [variant.pins]\n    1 = "C1"\n    2 = "C2"\n    3 = "C3"\n'
        '    5 = "C5"\n    6 = "C6"\n    7 = "C7"\n')
    var = P.Document(ref, tomlite.load(ref)).variants[0]
    names = {"1": "VCC", "2": "RST", "3": "CLK", "5": "GND", "6": "VPP", "7": "I/O"}
    bad = lambda n: [x["pin"] for x in P.compare(var, [
        N.Pin(k, v, "passive") for k, v in n.items()])
        if x["code"] == "pin_name_mismatch"]
    check(not bad(names), "sim: C1..C7 match the ISO 7816 signal names",
          bad(names))
    check(bad(dict(names, **{"2": "CLK", "3": "RST"})) == ["2", "3"],
          "sim: swapped RST and CLK are still caught")


_CHOKE_SYM = """(symbol "Lib:Choke"
  (symbol "Choke_0_1"
    (circle (center -1.5 2) (radius 0.2) (fill (type outline)))
    (circle (center -1.5 -1) (radius 0.2) (fill (type outline)))
    (arc (start -2.54 2.54) (mid 0 3) (end 2.54 2.54))
    (polyline (pts (xy -2.54 -2.54) (xy 0 -2.54) (xy 2.54 -2.54))))
  (symbol "Choke_1_1"
    (pin passive line (at -5.08 2.54 0) (length 2.54) (name "1") (number "1"))
    (pin passive line (at -5.08 -2.54 0) (length 2.54) (name "2") (number "2"))
    (pin passive line (at 5.08 -2.54 180) (length 2.54) (name "3") (number "3"))
    (pin passive line (at 5.08 2.54 180) (length 2.54) (name "4") (number "4"))))"""

_DUAL_SYM = """(symbol "Lib:Dual"
  (symbol "Dual_1_1"
    (polyline (pts (xy 0 2.54) (xy 0 -2.54)))
    (pin passive line (at 0 5.08 270) (length 2.54) (name "D") (number "6"))
    (pin input line (at -5.08 0 0) (length 2.54) (name "G") (number "2"))
    (pin passive line (at 0 -5.08 90) (length 2.54) (name "S") (number "1")))
  (symbol "Dual_2_1"
    (polyline (pts (xy 0 2.54) (xy 0 -2.54)))
    (pin passive line (at 0 5.08 270) (length 2.54) (name "D") (number "3"))
    (pin input line (at -5.08 0 0) (length 2.54) (name "G") (number "5"))
    (pin passive line (at 0 -5.08 90) (length 2.54) (name "S") (number "4"))))"""


def test_symbol_drawing_is_read():
    """Windings, dots and units come from the symbol's drawing. Units are
    drawn on top of each other, so each is traced on its own."""
    import symgraph as SG
    choke = SG.Symbol("Lib:Choke", _CHOKE_SYM)
    check(choke.joined == [["1", "4"], ["2", "3"]],
          "symbol: a choke's two windings are traced", choke.joined)
    check(choke.dots == ["1", "2"], "symbol: the dots belong to pins 1 and 2",
          choke.dots)
    dual = SG.Symbol("Lib:Dual", _DUAL_SYM)
    check(dual.unit_of() == {"6": "A", "2": "A", "1": "A",
                             "3": "B", "5": "B", "4": "B"},
          "symbol: a dual part's pins are split by unit", dual.unit_of())
    check(dual.joined == [["1", "6"], ["3", "4"]],
          "symbol: units are traced apart, never joined across", dual.joined)


def test_dual_part_units_must_be_one_device():
    """A dual FET symbol says S G D twice; the datasheet says S1 G1 D1 and S2
    G2 D2. Which device is which unit does not matter, a mix does."""
    import symgraph as SG
    dual = SG.Symbol("Lib:Dual", _DUAL_SYM).unit_of()
    names = {"1": "S", "2": "G", "3": "D", "4": "S", "5": "G", "6": "D"}
    pins = [N.Pin(n, v, "passive", dual[n]) for n, v in names.items()]

    def var(table):
        return P.Variant("X", {"mpns": ["M"], "pins": table})
    ok = var({"1": "S2", "2": "G2", "3": "D1", "4": "S1", "5": "G1", "6": "D2"})
    codes = {f["code"] for f in P.compare(ok, pins)}
    check(codes == {"pin_name_convention"},
          "dual: unit A = device 2 throughout is fine", codes)
    bad = var({"1": "S1", "2": "G1", "3": "D1", "4": "S2", "5": "G2", "6": "D2"})
    mixed = [f["pin"] for f in P.compare(bad, pins)
             if f["code"] == "pin_unit_mismatch"]
    check(sorted(mixed) == ["1", "2", "3", "4", "5", "6"],
          "dual: a drain wired to the other device is caught", mixed)
    plain = [N.Pin(n, v, "passive") for n, v in names.items()]
    check(any(f["code"] == "pin_name_mismatch" for f in P.compare(ok, plain)),
          "dual: without units S vs S2 stays a difference to judge")


def test_choke_connections_against_symbol():
    """A numbered-only part: the datasheet's windings, dots and pad layout
    against the symbol's drawing."""
    import symgraph as SG
    choke = SG.Symbol("Lib:Choke", _CHOKE_SYM)

    def var(**kw):
        return P.Variant("X", dict({"mpns": ["M"],
                                    "pin_numbers": ["1", "2", "3", "4"],
                                    "pad_layout": "same"}, **kw))
    codes = lambda v: [f["code"] for f in P.connection_findings(v, choke)]
    check(codes(var(joined=[["1", "4"], ["2", "3"]], dots=["1", "2"])) == [],
          "choke: same windings and dots agree")
    check(codes(var(joined=[["1", "4"], ["2", "3"]], dots=["4", "3"])) == [],
          "choke: every dot at the other end is the same coupling")
    check(codes(var(joined=[["1", "4"], ["2", "3"]], dots=["1", "3"])) ==
          ["dots_differ"], "choke: one winding reversed is caught")
    check(codes(var(joined=[["1", "2"], ["3", "4"]])) == ["connections_differ"],
          "choke: windings 1-2 3-4 against a 1-4 2-3 symbol are caught")
    check(codes(var(joined=[["1", "4"], ["2", "3"]], pad_layout="differs")) ==
          ["pad_layout_differs"], "choke: a footprint numbered otherwise is a finding")
    blank = SG.Symbol("Lib:Box", '(symbol "Lib:Box" (symbol "Box_1_1" '
                      '(rectangle (start -2 2) (end 2 -2)) '
                      '(pin passive line (at -4 0 0) (length 2) (name "1") (number "1")) '
                      '(pin passive line (at 4 0 180) (length 2) (name "2") (number "2"))))')
    v = var(joined=[["1", "2"]])
    check([f["code"] for f in P.connection_findings(v, blank)] ==
          ["connections_unread"], "choke: a drawing without lines goes to a person")
    v.symbol_checked = "Lib:Box"
    check(P.connection_findings(v, blank) == [],
          "choke: once a person compared it, it is settled")


def test_wizard_asks_a_chokes_windings():
    """The wizard asks the windings, the dots and the pad layout from the
    datasheet, writes them in the table, and compares with the symbol."""
    import tempfile
    import symgraph as SG
    import wizard as WZ
    att = os.path.join(tempfile.mkdtemp(), "attestations")
    os.makedirs(att)
    path = os.path.join(att, "M.toml")
    open(path, "w").write('doc_id = "X"\norigin = "manual"\nextracted_by = "T"\n'
                          'reviewed_by = "T"\nreviewed_on = "2026-01-01"\n\n'
                          '  [[variant]]\n  mpns = ["M"]\n'
                          '  pin_numbers = ["1", "2", "3", "4"]\n')

    class Comp:
        pins = [N.Pin(n, n, "passive") for n in "1234"]
        libsource, footprint, value = "Lib:Choke", "F:F", "L"

        def field(self, name):
            return ""

    class Line:
        mpn, refs, comp = "M", ["L1"], Comp()
        symbol = SG.Symbol("Lib:Choke", _CHOKE_SYM)
    WZ._find_pdf = lambda *a: ""
    answers = iter(["1-5", "1-4 2-3", "1 2", "y"])
    said = []
    w = WZ.Wizard(att, att, [], [], ask=lambda p: next(answers),
                  say=said.append, auto_open=False, by="T")
    w.do_connections_unfilled(TD_Item("connections_unfilled", "M", ["L1"], path),
                              Line())
    var = P.Document(path, tomlite.load(path)).variants[0]
    check(var.joined == [["1", "4"], ["2", "3"]] and var.dots == ["1", "2"]
          and var.pad_layout == "same", "wizard: windings, dots, layout saved",
          open(path).read())
    check(any("Not a pin of this part: 5" in x for x in said),
          "wizard: a pin the part does not have is refused")
    check(any("connects the same pins" in x for x in said),
          "wizard: the answer is compared with the symbol at once", said[-3:])


def TD_Item(*a, **k):
    import todo as TD
    return TD.Item(*a, **k)


def test_marking_question_and_footprint_mark():
    """The datasheet's mark is asked in two steps: which mark, then which
    pin. A + on the footprint is the + pin whatever the part shows; a band
    on the footprint is where the part's own mark goes."""
    import polarity as PO
    import wizard as WZ

    class Cap:
        pins = [N.Pin("1", "~", "passive"), N.Pin("2", "~", "passive")]

    class Diode:
        pins = [N.Pin("1", "K", "passive"), N.Pin("2", "A", "passive")]
    cap, diode = Cap(), Diode()
    check(PO.marked_pin(cap, PO.POLAR_CAP, "minus", plus_sign=True) == "1",
          "mark: a + on the footprint is the + pin, the band on the part aside")
    check(PO.marked_pin(cap, PO.POLAR_CAP, "minus") == "2",
          "mark: a band on the footprint of a minus-band cap is the - pin")
    check(PO.marked_pin(cap, PO.POLAR_CAP, "plus") == "1",
          "mark: a tantalum's band is the + pin")
    check(PO.marked_pin(diode, PO.POLAR_DIODE, "anode") == "2",
          "mark: a diode marked at its anode")
    check(PO.marked_pin(diode, PO.POLAR_DIODE) == "1",
          "mark: no answer yet, KiCad's convention (the cathode)")

    def run(klass, answers):
        feed = iter(answers)
        w = WZ.Wizard("/x", "/x", [], [], ask=lambda p: next(feed),
                      say=lambda t: None, auto_open=False)
        return w.ask_marking(klass, "", "")
    check(run(PO.POLAR_CAP, ["p"]) == ("plus sign", "plus"),
          "mark: a + sign on a cap needs no second question")
    check(run(PO.POLAR_CAP, ["b", "m"]) == ("band", "minus"),
          "mark: a band on the - pin")
    check(run(PO.POLAR_DIODE, ["o", "notch", "c"]) == ("other: notch", "cathode"),
          "mark: another mark is described")
    check(run(PO.BIDIRECTIONAL, ["n"]) == ("", "none"),
          "mark: no polarity, one question")
    check(run(PO.POLAR_CAP, ["b", "o", "the datasheet does not say"]) ==
          ("band", "other: the datasheet does not say"),
          "mark: the pin can be described when no choice fits")
    import todo as TD

    class Ln:
        mpn, table_path, draft_path = "M", "x.toml", None
        comps = [type("C", (), {"ref": "C1", "pins": cap.pins, "libpart": None,
                                "libsource": "Device:C_Polarized"})()]
    var = P.Variant("X", {"mpns": ["M"], "marked": "other: not shown"})
    codes = [i.code for i in TD.datasheet_items(Ln(), var)]
    check(codes == ["polarity.mark_unclear"],
          "mark: a described pin goes to a person", codes)


def test_alias_is_an_equivalence_not_a_waiver():
    """Accepting a rename records `symbol name == datasheet name` for that part.
    It is NOT "this pin is fine from now on", and the difference matters: a
    waiver would hide the very defect class this exists to catch.

    Asserted by breaking the symbol three ways after accepting."""
    import tempfile
    ref = os.path.join(tempfile.mkdtemp(), "R.toml")
    open(ref, "w", encoding="utf-8").write(
        'doc_id = "X"\n\n  [[variant]]\n  package = "P"\n  mpns = ["M"]\n'
        '\n    [variant.pins]\n    1 = "VFB"\n    2 = "COMP"\n    3 = "AGND"\n')
    pins = lambda **kw: [N.Pin(n, kw.get(n, d), "passive")
                         for n, d in (("1", "FB"), ("2", "COMP"), ("3", "AGND"))]
    load = lambda: P.Document(ref, tomlite.load(ref)).variants[0]
    bad = lambda ps: [x["msg"] for x in P.compare(load(), ps)
                      if x["code"] in ("pin_name_mismatch", "pin_polarity")]

    check(len(bad(pins())) == 1, "alias: the difference is reported first")
    DC.add_alias(ref, "M", "1", "FB", "VFB")
    check(not bad(pins()), "alias: accepted, so it stops being reported")

    # ...but the symbol can still be wrong in every way that matters.
    check(bad(pins(**{"1": "GND"})),
          "alias: renaming that pin to something else is still caught")
    check(len(bad(pins(**{"1": "COMP", "2": "FB"}))) == 2,
          "alias: SWAPPING two pins is still caught -- both of them",
          str(bad(pins(**{"1": "COMP", "2": "FB"}))))
    check(bad(pins(**{"3": "FB"})),
          "alias: the same name on a different pin is still caught")

    # A dual FET symbol calls both sources S; the datasheet says S1 and S2.
    # Name-keyed aliases collided here, per-pin ones do not.
    fet = os.path.join(tempfile.mkdtemp(), "F.toml")
    open(fet, "w", encoding="utf-8").write(
        'doc_id = "X"\n\n  [[variant]]\n  package = "P"\n  mpns = ["F"]\n'
        '\n    [variant.pins]\n    1 = "S1"\n    4 = "S2"\n')
    fpins = [N.Pin("1", "S", "passive"), N.Pin("4", "S", "passive")]
    DC.add_alias(fet, "F", "1", "S", "S1")
    DC.add_alias(fet, "F", "4", "S", "S2")
    DC.add_alias(fet, "F", "4", "S", "S2")
    var = P.Document(fet, tomlite.load(fet)).variants[0]
    check(not [x for x in P.compare(var, fpins) if x["code"] == "pin_name_mismatch"],
          "alias: two pins with the same symbol name are both accepted",
          open(fet).read())
    check(open(fet).read().count("4 =") == 2, "alias: re-accepting does not duplicate",
          open(fet).read())


def test_partial_fill_is_not_a_conflict():
    """One part number cannot have two datasheets. When some placements carry a
    value and the rest are empty, that is an incomplete FILL, not a
    disagreement, and the value that exists should be proposed for the rest.

    On this board 3 of SMA6J13A-TR's 16 placements carry a datasheet URL. The
    row used to read `(varies)` and propose nothing, which is a shrug rather
    than an answer. Only two DIFFERENT non-empty values are a real conflict,
    because then choosing one would be a guess about which placement is right.
    """
    # Same value on some, empty on others -> propose it.
    vals = ["", "", "https://x/y.pdf", ""]
    filled = sorted({v for v in vals if v})
    check(len(filled) == 1, "partial-fill: one distinct value")
    row = DC.field_row("M", ["D1", "D2"], {"Datasheet": ""},
                       {"Datasheet": filled[0]})
    check(row["action"] == "add",
          "partial-fill: reported as add, not REPLACE or mixed", row["action"])
    check(row["datasheet_set"] == "https://x/y.pdf",
          "partial-fill: the existing value is proposed for the rest")

    # Two different values IS a conflict -> propose nothing.
    row2 = DC.field_row("M", ["D1", "D2"], {"Datasheet": DC._MIXED}, {})
    check(row2["action"] == "mixed" and not row2["datasheet_set"],
          "partial-fill: a genuine conflict proposes nothing", str(row2["action"]))


def test_decisions_survive_regeneration():
    """Re-running review must not discard decisions already made.

    It used to overwrite the file, so every mark made since the last run was
    silently destroyed -- which turns "run it again to pick up your symbol fix"
    into "lose an afternoon of decisions"."""
    import tempfile
    p = os.path.join(tempfile.mkdtemp(), "pins.csv")
    DC.write(p, [DC.pin_row("M", "U8", "22", "FB", "VFB", "likely rename"),
                 DC.pin_row("M", "U8", "25", "IL", "ILIM", "likely rename")],
             DC.PIN_COLS)
    txt = open(p, encoding="utf-8-sig").read().replace("\n,M,U8,22", "\ny,M,U8,22")
    open(p, "w", encoding="utf-8").write(txt)

    fresh = [DC.pin_row("M", "U8", "22", "FB", "VFB", "likely rename"),
             DC.pin_row("M", "U8", "25", "IL", "ILIM", "likely rename"),
             DC.pin_row("M", "U9", "1", "A", "B", "needs judgement")]
    kept, n = DC.carry_over(p, fresh, ["part", "ref", "pin"], ["accept"])
    check(n == 1, "carry-over: the mark is preserved", str(n))
    check(kept[0]["accept"] == "y", "carry-over: on the right row",
          str(kept[0]["accept"]))
    check(kept[1]["accept"] == "" and kept[2]["accept"] == "",
          "carry-over: unmarked rows stay unmarked, new rows start clean")

    # A CORRECTED value wins over a freshly proposed one -- the person may have
    # edited it rather than merely accepted it.
    f = os.path.join(os.path.dirname(p), "fields.csv")
    DC.write(f, [DC.field_row("M", ["U1"], {}, {"Manufacturer": "TI"})],
             DC.FIELD_COLS)
    txt = open(f, encoding="utf-8-sig").read().replace(",TI,", ",Texas Instruments,")
    open(f, "w", encoding="utf-8").write(txt)
    kept2, n2 = DC.carry_over(f, [DC.field_row("M", ["U1"], {},
                                               {"Manufacturer": "TI"})],
                              ["part"], ["mfr_set"])
    check(kept2[0]["mfr_set"] == "Texas Instruments",
          "carry-over: a corrected value is not overwritten by the proposal",
          kept2[0]["mfr_set"])


def test_decisions_round_trip():
    """One row per PART, one column pair per field: `_now` is what the design
    says, `_set` is what to write. An empty `_set` means no change, so
    rejecting a suggestion is deleting a cell.

    The pairing is what makes a change to an EXISTING value visible by
    construction rather than by convention."""
    import tempfile
    d = tempfile.mkdtemp()
    fp = os.path.join(d, "decisions-fields.csv")
    DC.write(fp, [
        DC.field_row("MCP9701AT-E/LT", ["U16"], {},
                     {"Manufacturer": 'Würth, Inc. "x"'}),
        DC.field_row("TXU0204PWR", ["U4", "U5"],
                     {"Datasheet": "https://ti.com/txu0304.pdf"},
                     {"Datasheet": "https://ti.com/txu0204.pdf"}),
        DC.field_row("691322310003", ["J4"], {"MPN": "691322310003"}, {}),
        DC.field_row("SMA6J13A-TR", ["D3", "D4"], {"Datasheet": DC._MIXED}, {}),
    ], DC.FIELD_COLS)
    rows = list(csv.DictReader(open(fp, newline="", encoding="utf-8-sig")))
    check([r["action"] for r in rows] == ["add", "REPLACE", "-", "mixed"],
          "decisions: action names add / REPLACE / mixed",
          str([r["action"] for r in rows]))

    edits = DC.parse_fields(fp)
    check(len(edits) == 2, "decisions: only non-empty _set cells are edits",
          str(len(edits)))
    check(edits[0].value == 'Würth, Inc. "x"',
          "decisions: commas, quotes and non-ASCII survive", repr(edits[0].value))
    check(edits[0].refs == ["U16"] and edits[1].refs == ["U4", "U5"],
          "decisions: every designator of a shared row is carried",
          str(edits[1].refs))
    check(not edits[0].is_replace and edits[1].is_replace,
          "decisions: replacing an existing value is distinguishable from "
          "filling an empty one")

    # An all-numeric MPN is written apostrophe-forced, so a spreadsheet cannot
    # turn 691322310003 into 6.91322e+11 the moment the file is opened.
    raw = open(fp, encoding="utf-8-sig").read()
    check("'691322310003" in raw, "decisions: long digit strings forced to text")

    # ...and a value that came back reformatted is refused, not written.
    open(fp, "w", newline="", encoding="utf-8").write(
        ",".join(DC.FIELD_COLS) + "\n"
        "X,J4,add,,,,,,6.91322E+11,\n")
    e = DC.parse_fields(fp)[0]
    check(e.problem and "refusing" in e.problem,
          "decisions: a reformatted number is refused", e.problem)

    # Pins default to NOT accepted: silencing a pin difference is a claim about
    # the design, unlike filling in a missing manufacturer.
    pp = os.path.join(d, "decisions-pins.csv")
    DC.write(pp, [DC.pin_row("SIC473ED-T1-GE3", "U8", "22", "FB", "VFB",
                             "likely rename", "contained")], DC.PIN_COLS)
    pins = DC.parse_pins(pp)
    check(len(pins) == 1 and not pins[0].accepted,
          "decisions: a pin row is not accepted until marked", str(pins))
    txt = open(pp, encoding="utf-8-sig").read().replace("\n,SIC", "\nyes,SIC")
    open(pp, "w", encoding="utf-8").write(txt)
    check(DC.parse_pins(pp)[0].accepted,
          "decisions: any mark accepts a pin row")


def test_bom_field_suggestions():
    """The schematic should be the source of truth for Manufacturer, MPN and
    datasheet URL. Suggestions carry their evidence, and refuse to guess."""
    check(SG.vendor_from_url("https://www.ti.com/lit/ds/x.pdf")[0]
          == "Texas Instruments", "suggest: host identifies the manufacturer")
    check(SG.vendor_from_url("https://www.we-online.com/x.pdf")[0]
          == "Würth Elektronik",
          "suggest: www. is stripped as a PREFIX, not as characters",
          str(SG.vendor_from_url("https://www.we-online.com/x.pdf")))
    check(SG.vendor_from_url("https://docs.rs-online.com/x.pdf")[0] is None,
          "suggest: a DISTRIBUTOR host names no manufacturer")
    check(SG.vendor_from_text("... TEXAS INSTRUMENTS ...")[0]
          == "Texas Instruments", "suggest: text corroborates the host")

    # MPN taken from Value when it plausibly holds one...
    q3 = N.Comp("Q3", "BC857BS", {}, {}, "", "x:y")
    s1 = SG.for_component(q3, "https://www.nexperia.com/BC857BS.pdf", "",
                          "confirmed")
    by = {x.field: x for x in s1}
    check(by["MPN"].value == "BC857BS" and by["MPN"].confidence == "medium",
          "suggest: MPN proposed from the Value field", str(s1))
    check(by["Manufacturer"].value == "Nexperia"
          and by["Manufacturer"].confidence == "high",
          "suggest: manufacturer from the datasheet host", str(s1))

    # ...and NEVER when it holds a functional name.
    j1 = N.Comp("J1", "swd_with_uart_client", {}, {}, "", "x:y")
    by2 = {x.field: x for x in SG.for_component(j1, "", "", "")}
    check(by2["MPN"].value == "" and by2["MPN"].confidence == "none",
          "suggest: refuses to invent an MPN from a functional name",
          str(by2["MPN"]))

    # A part with everything already filled produces nothing.
    full = N.Comp("U1", "X", {"MPN": "X", "Manufacturer": "Y",
                              "Datasheet": "https://z/x.pdf"}, {}, "", "x:y")
    check(SG.for_component(full, "https://z/x.pdf", "", "confirmed") == [],
          "suggest: silent when the fields are already there")


def test_family_stem_must_be_distinctive():
    """A "series document" verdict rests on finding the part's family stem in
    the text. Shortening the order code without a distinctiveness rule reaches
    "1500" for Wuerth's 150060VS75000 -- and "1500" occurs in any document with
    a number in it. A Grundfos pump manual sitting in this project's datasheet
    folder verified as a series document for a terminal block on that basis.

    A stem must therefore mix letters and digits. That keeps EC21 and EG915N
    working while rejecting bare numbers."""
    pump = "Pump speed 1500 rpm at 1500 W. " * 40
    v = D.verify("150060VS75000", pump, "")
    check(v["verdict"] == "wrong-part",
          "stem: a bare number is not a family stem", v["verdict"])

    real = "EG915N module. " * 60 + "The EG915N supports LTE Cat 1. " * 20
    v2 = D.verify("EG915NEAAC-N05-SNNSA", real, "")
    check(v2["verdict"] == "family-only",
          "stem: EG915N is found by shortening the order code", v2["verdict"])
    check("EG915N" in v2["why"], "stem: reports which stem matched", v2["why"])

    # A fixed pattern cannot do this: "^[A-Z]+[0-9]+" yields EG915 here (one
    # character short) and S25 for S25FL064LABMFI013 (far too short).
    v3 = D.verify("S25FL064LABMFI013", "S25FL064L flash memory. " * 30, "")
    check(v3["verdict"] == "family-only",
          "stem: S25FL064L found, not S25", v3["why"][:60])


def test_dnp_parts_are_out_of_scope():
    """A do-not-populate part is not fitted and not on the BOM, so demanding an
    MPN and a datasheet for it is noise. J2 on this board is a DNI U.FL
    connector and was being reported as a missing datasheet."""
    dnp = N.Comp("J2", "U.FL-R-SMT-1(10)", {}, {"dnp": ""}, "", "x:y")
    check(dnp.not_placed, "dnp: native flag recognised")
    legacy = N.Comp("J9", "DNI", {}, {}, "", "x:y")
    check(legacy.not_placed, "dnp: legacy DNI value recognised")
    field = N.Comp("J8", "CONN", {"DNP": "yes"}, {}, "", "x:y")
    check(field.not_placed, "dnp: legacy field recognised")
    live = N.Comp("U1", "STM32L4R5VIT6", {}, {}, "", "x:y")
    check(not live.not_placed, "dnp: a fitted part is in scope")


def test_subject_check_catches_a_mislinked_datasheet():
    """A part number being PRESENT is not the same as the document being ABOUT
    it. This board links TXU0204PWR to txu0304.pdf, which names TXU0304 ~75
    times and TXU0204 four times in a related-products table. The exact-token
    test says "family-only", extraction then reads TXU0304's pin map, and pin 4
    comes out A3 for a part whose pin 4 is A3Y.

    The mirror case matters just as much: TPD4E05U06QDQARQ1 links to
    tpdxe05u06-q1.pdf, which IS its family datasheet. Flagging that would block
    a perfectly good document."""
    doc_wrong = ("TXU0304 datasheet. " * 75) + "See also TXU0204 and TXU0104. "
    ok, why = D.subject_check("TXU0204PWR", doc_wrong)
    check(not ok, "subject: a document about a sibling part is rejected", why)
    check("TXU0304" in why, "subject: names the part it is actually about", why)

    doc_right = ("TPD4E05U06 device. " * 55) + "TPDxE05U06-Q1 family. " * 5
    ok2, why2 = D.subject_check("TPD4E05U06QDQARQ1", doc_right)
    check(ok2, "subject: the part's own family datasheet is accepted", why2)

    # A single digit inside the letters must not make a part invisible.
    check(D._PARTLIKE.search("TPD4E05U06"),
          "subject: TPD4E05U06 is recognised as a part number")


def test_mpn_falls_back_to_value_but_says_so():
    """Some symbols carry the part number in Value with no MPN field -- Q3 is
    BC857BS. Refusing to look means the part cannot be validated at all. But the
    fallback must be able to say no: J1's value is `swd_with_uart_client`, and
    accepting that sends the tool hunting for a datasheet that cannot exist."""
    for good in ("BC857BS", "490107670612", "STM32L4R5VIT6", "U.FL-R-SMT-1(10)"):
        check(N.looks_like_mpn(good), f"mpn-from-value: {good!r} is plausible")
    for bad in ("swd_with_uart_client", "100n", "2.2u", "10k", "32.768kHz",
                "4700", "DNP"):
        check(not N.looks_like_mpn(bad),
              f"mpn-from-value: {bad!r} is NOT a part number")

    c = N.Comp("Q3", "BC857BS", {}, {}, "", "Transistor_BJT:BC857BS")
    check(c.mpn_effective == ("BC857BS", "value-field"),
          "mpn-from-value: used, and the source is reported",
          str(c.mpn_effective))
    j = N.Comp("J1", "swd_with_uart_client", {}, {}, "", "x:y")
    check(j.mpn_effective == ("", "none"),
          "mpn-from-value: a functional name yields no MPN", str(j.mpn_effective))


def test_stage_one_never_sees_the_symbol():
    """Extraction answers "what does this datasheet say for this MPN?" and must
    be answerable without the symbol. If the symbol could steer it, a symbol
    wired to the wrong part would drag the evidence along with it -- exactly the
    failure this project exists to catch.

    Enforced by the API: Context carries no symbol-derived field, and the pin
    count it does carry comes from the ORDER CODE or the footprint's package
    size, never from the symbol's pin table."""
    fields = set(T.Context.__slots__)
    for banned in ("symbol", "symbol_pins", "libpart", "sym_pins", "netlist"):
        check(banned not in fields,
              f"stage 1: Context has no {banned!r} field", str(sorted(fields)))
    check(T.Context.package_pin_count("Package_QFP:LQFP-100_14x14mm") == 100,
          "stage 1: pin count read from the footprint's package size")
    check(T.Context.package_pin_count("Package_SO:TSSOP-16_4.4x5mm") == 16,
          "stage 1: ...and for a TSSOP")
    # JEDEC dimensional naming puts the BODY SIZE after the family and the pin
    # count at the end. Reading the first number makes an 8-pin DFN a 200-pin
    # part, which then reports 192 missing pins.
    check(T.Context.package_pin_count("Jitter_IC:DFN200X150X80-8N") == 8,
          "stage 1: dimensional footprint names yield the pin count, not the size",
          str(T.Context.package_pin_count("Jitter_IC:DFN200X150X80-8N")))
    check(T.Context.package_pin_count("Jitter_IC:Quectel_LGA126_23.6x19.9x2.4")
          == 126, "stage 1: a family+count name still reads")
    check(T.Context.package_pin_count("Package_TO_SOT_SMD:SOT-353") == 0,
          "stage 1: SOT/SC JEDEC codes are refused, not mistaken for counts")
    check(T.Context.package_pin_count("Package_TO_SOT_SMD:SOT-353_SC-70-5") == 5,
          "stage 1: ...but a lead count after the code is read")
    check(T.Context.package_pin_count("Package_TO_SOT_SMD:TSOT-23-5") == 5,
          "stage 1: TSOT-23-5 is five pins")
    check(T.Context.footprint_exposed_pads(
        "Jitter_IC:Texas_DGN0008K_VSSOP-8-1EP_3x3mm") == 1,
          "stage 1: an exposed pad is read from the footprint name")

    # Same datasheet, same MPN, two Contexts differing only in a package hint
    # that is WRONG -- the reading must not silently adapt to it.
    txt = os.path.join(HERE, "fixtures", "tmux1309a_p7.txt")
    if os.path.isfile(txt):
        text = open(txt, encoding="utf-8").read()
        a = T.read_pin_table(text, T.Context(mpn="TMUX1309APWR",
                                             package="TSSOP16", expect_pins=16))
        b = T.read_pin_table(text, T.Context(mpn="TMUX1309APWR",
                                             package="TSSOP16", expect_pins=0))
        check(a["pins"] == b["pins"],
              "stage 1: the reading does not change with the expected count")


def test_one_document_two_parts():
    """SCDS477A publishes BOTH TMUX1308A (single 8:1) and TMUX1309A (dual 4:1)
    in the same TSSOP-16, with genuinely different pinouts. The MPN alone must
    pick the right one -- reading the wrong table gives a complete, confident,
    wrong pin map of exactly the kind that caused the original incident."""
    txt = os.path.join(HERE, "fixtures", "tmux_both_variants.txt")
    if not os.path.isfile(txt):
        check(True, "two-parts: skipped, no cached document")
        return
    text = open(txt, encoding="utf-8").read()
    nine = T.read_pin_table(text, T.Context(mpn="TMUX1309APWR",
                                            package="TSSOP16", expect_pins=16))
    eight = T.read_pin_table(text, T.Context(mpn="TMUX1308APWR",
                                             package="TSSOP16", expect_pins=16))
    check(nine["scope"] == "TMUX1309A" and eight["scope"] == "TMUX1308A",
          "two-parts: each MPN scopes to its own table",
          f"{nine['scope']} / {eight['scope']}")
    check(nine["pins"].get("1") == "S0B", "two-parts: 1309A pin 1 is S0B",
          str(nine["pins"].get("1")))
    check(eight["pins"].get("1") == "S4", "two-parts: 1308A pin 1 is S4",
          str(eight["pins"].get("1")))
    check(nine["pins"] != eight["pins"],
          "two-parts: the two pin maps genuinely differ")
    check(nine["pins"].get("3") == "DB" and eight["pins"].get("3") == "D",
          "two-parts: pin 3 is DB on the dual, D on the single")


def test_shape_number_name_only():
    """Rows that are just NUMBER + NAME, description on other lines.

    Trivially easy to match by accident -- "1 Introduction" has the same form --
    so the guards are the test: exactly two cells, number inside the pin range,
    a pin-like name, and at least four rows together."""
    txt = os.path.join(HERE, "fixtures", "sic473_pindesc.txt")
    if not os.path.isfile(txt):
        check(True, "number_name_only: skipped, no cached section")
        return
    ctx = T.Context(mpn="SIC473ED-T1-GE3", expect_pins=30)
    r = T.shape_number_name_only(open(txt, encoding="utf-8").read(), ctx)
    check(r.pins.get("1") == "VCIN" and r.pins.get("2") == "PGOOD",
          "number_name_only: reads the two-cell rows", str(sorted(r.pins)[:4]))
    # The fixture is a slice of the section, so this is the count IT holds --
    # the full datasheet yields 12 through the driver.
    check(len(r.pins) >= 8, "number_name_only: reads the rows in this slice",
          str(len(r.pins)))

    prose = "1 Introduction\n2 Features\n3 Description\n4 Applications\n"
    check(not T.shape_number_name_only(prose, ctx).pins,
          "number_name_only: a contents list is not a pin table")

    # A register table numbers bits, and its field names look like pin names.
    # S25FL064L's status register read as pins 1-7 of a SOIC-8.
    reg = ("Bits    Field name    Function     Type\n"
           "  7     SRP0_NV     Status Register   Non-volatile\n"
           "  6     SEC_NV      Sector / Block    Non-volatile\n"
           "  5     TBPROT_NV   TBPROT Default    Non-volatile\n"
           "  2     BP_NV0      Legacy Block      Non-volatile\n")
    check(not T.shape_number_name_only(reg, T.Context(expect_pins=8)).pins,
          "number_name_only: a register bit table is not a pin table")


def test_name_then_columns_uses_ordinal_position():
    """Header text is not aligned with the data beneath it, and one label may
    name several packages: TI heads a column "PW, BQA" starting eight columns
    left of the numbers it labels. Selecting by x offset misses; selecting by
    the column's ordinal position, after clustering the label tokens, works."""
    txt = os.path.join(HERE, "fixtures", "txu0204_pinfn.txt")
    if not os.path.isfile(txt):
        check(True, "ordinal: skipped, no cached section")
        return
    text = open(txt, encoding="utf-8").read()
    ctx = T.Context(mpn="TXU0204PWR", footprint="Package_SO:TSSOP-14_4.4x5mm",
                    expect_pins=14)
    r = T.shape_name_then_columns(text, ctx)
    check(r.pins.get("2") == "A1" and r.pins.get("13") == "B1Y",
          "ordinal: reads the PW column", str(sorted(r.pins.items())[:3]))
    check("position 1 of 2" in r.note, "ordinal: reports which column it used",
          r.note)

    # The other package's column must give a DIFFERENT mapping.
    other = T.Context(mpn="TXU0204RUTR", footprint="UQFN", expect_pins=14)
    r2 = T.shape_name_then_columns(text, other)
    check(r2.pins.get("10") == "B1Y",
          "ordinal: the RUT column maps B1Y to pin 10, not 13", str(r2.pins))


def test_non_numeric_pin_ids_rejected():
    """A numbered package cannot have a pin called B4Y. Without this guard the
    TXU table's logic-name column was accepted as pin numbers, producing
    entries like {"B4Y": "A4"}."""
    def bogus(text, c):
        return T.Result("row_per_pin", {"1": "VCC", "B4Y": "A4", "A1": "A1"})
    ctx = T.Context(mpn="X", footprint="Package_SO:TSSOP-14", expect_pins=14)
    check(ctx.numeric_pins, "numeric_pins: TSSOP is a numbered package")
    out = T.read_pin_table("x", ctx, shapes=(bogus,))
    check(out["pins"] == {"1": "VCC"}, "reject: only the numeric id survives",
          str(out["pins"]))
    bga = T.Context(mpn="X", footprint="Package_BGA:UFBGA-169", expect_pins=169)
    check(not bga.numeric_pins, "numeric_pins: BGA pins are row+column")


def test_column_shape_needs_two_real_columns():
    """Two guards against a single-column table being read as a multi-package
    one, each from a real misread:

      * after clustering, header tokens may collapse to ONE column -- they were
        a single label all along. Reading that as multi-package produced the
        TMUX1309A pin names under a scope correctly narrowed to TMUX1308A.
      * a dash marks "absent in this package" and is not a column of numbers.
        TPS1H200A is a plain NAME/NO./TYPE table whose TYPE is an em dash for
        GND, and counting it produced a partial reading that then GATED the
        row shape which reads the table correctly.
    """
    single = ("  NAME   NO.   TYPE   DESCRIPTION\n"
              "  CL     4     O      Adjustable current limit\n"
              "  GND    6     —      Ground\n"
              "  IN     1     I      Input control\n")
    ctx = T.Context(mpn="TPS1H200AQDGNRQ1", footprint="VSSOP-8", expect_pins=8)
    r = T.shape_name_then_columns(single, ctx)
    check(not r.pins, "column shape: a single-column table is not read as multi",
          str(r.pins))

    # The genuine two-column case still works.
    # Column gaps must exceed the clustering distance, as they do in a real
    # document -- tokens closer than that are one label, e.g. "PW, BQA".
    dual = ("  NAME           X2SON            SOT-23        I\n"
            "  IN               4                 1          I\n"
            "  OUT              1                 5          O\n"
            "  GND              2                 2          -\n")
    ctx2 = T.Context(mpn="TPS7A2033PDBVR", footprint="TSOT-23-5", expect_pins=5)
    r2 = T.shape_name_then_columns(dual, ctx2)
    check(r2.pins.get("1") == "IN" and r2.pins.get("5") == "OUT",
          "column shape: a real two-column table still reads", str(r2.pins))


def test_gate_needs_a_nearly_complete_table():
    """A column shape only silences the generic shapes when it produced NEARLY
    ALL the pins. At half, a shape that matched some other two-column table in
    the document -- an ordering table, a device comparison -- would gate the
    simple row shape that reads the real one."""
    def partial(text, c):
        return T.Result("name_then_columns", {"1": "A", "2": "B", "3": "C",
                                              "4": "D"})

    def rows(text, c):
        return T.Result("row_per_pin", {str(i): f"P{i}" for i in range(1, 9)})

    out = T.read_pin_table("x", T.Context(mpn="X", expect_pins=8),
                           shapes=(partial, rows))
    check(len(out["pins"]) == 8,
          "gate: a half-complete column read does not silence the row shape",
          str(len(out["pins"])))

    # Ranking is by COVERAGE, not declaration order, so a complete row reading
    # legitimately outranks a 7-of-8 column reading. Order alone was the wrong
    # tie-breaker: on ADS114S08 it put REFP1/AIN6 on pin 9, where the real
    # table says CS.
    def near(text, c):
        return T.Result("number_columns", {str(i): f"C{i}" for i in range(1, 8)})
    out2 = T.read_pin_table("x", T.Context(mpn="X", expect_pins=8),
                            shapes=(near, rows))
    check(out2["pins"].get("8") == "P8",
          "gate: the better-covering reading wins regardless of order",
          str(out2["pins"].get("8")))
    check(any("coverage" in n for n in out2["notes"]),
          "gate: the ranking is reported", str(out2["notes"])[:80])


def test_driver_gates_scavengers():
    """Once a column table is read, generic row scanning must stop.

    Across a 308-page datasheet the generic shape collects rows from unrelated
    tables: the STM32 gained 51 spurious pins on top of a correct 98, tipping
    past the over-extraction guard and losing the result entirely."""
    doc = "\n".join(f"  P{i}  {i}  {i}  I/O  d" for i in range(1, 9))
    ctx = T.Context(mpn="X", footprint="AAA", expect_pins=8)

    def fake_cols(text, c):
        return T.Result("number_columns", {str(i): f"P{i}" for i in range(1, 9)})

    def scavenger(text, c):
        return T.Result("row_per_pin", {"7": "JUNK", "8": "JUNK"})

    out = T.read_pin_table(doc, ctx, shapes=(fake_cols, scavenger))
    check(out["pins"].get("7") == "P7" and "7" not in out["conflicts"],
          "driver: scavenger skipped after a column read",
          str(out["conflicts"].get("7")))
    check(any("skipped" in n for n in out["notes"]),
          "driver: the skip is reported, not silent", str(out["notes"]))

    # ...but a weak column result must NOT gate them.
    def weak_cols(text, c):
        return T.Result("number_columns", {"1": "A", "2": "B"})
    out2 = T.read_pin_table(doc, ctx, shapes=(weak_cols, scavenger))
    check(out2["pins"].get("7") == "JUNK",
          "driver: a weak column read does not gate")


def test_driver_refuses_overextraction():
    def many(text, c):
        return T.Result("row_per_pin", {str(i): f"P{i}" for i in range(1, 12)})
    out = T.read_pin_table("x", T.Context(mpn="X", expect_pins=5), shapes=(many,))
    check(out["overextracted"] == 11 and out["pins"] == {},
          "driver: 11 pins for a 5-pin part is refused", str(out["overextracted"]))


def test_package_scoped_tables():
    """A datasheet covering several packages often publishes ONE TABLE PER
    PACKAGE. Merging them is not a small error: TPS7A2033PDBVR is a 5-pin
    SOT-23 part whose document also holds a DSBGA table, and merging the two
    produced NINE pins for a five-pin part."""
    txt = os.path.join(HERE, "fixtures", "tmux1309a_p7.txt")
    if not os.path.isfile(txt):
        check(True, "scoped: skipped, no cached text")
        return
    doc = ("Table 5-1. Pin Functions TMUX1308A\n  S4  1  I/O  x\n"
           "Table 5-2. Pin Functions TMUX1309A\n  S0B  1  I/O  y\n")
    spans = X.section_spans(doc)
    check([l for l, _, _ in spans] == ["TMUX1308A", "TMUX1309A"],
          "scoped: 'Table N.' prefix does not hide the heading",
          str([l for l, _, _ in spans]))
    sel = X.choose_section(doc, ["", "TMUX1309APWR"])
    check(sel and sel[0] == "TMUX1309A", "scoped: picks the fitted part's table",
          str(sel and sel[0]))

    # A label may list several packages; each must be matched separately.
    doc2 = ("Pin Functions: DSBGA\n  IN  A1  I  x\n"
            "Pin Functions: X2SON, SOT-23\n  IN  1  I  y\n")
    sel2 = X.choose_section(doc2, ["Package_TO_SOT_SMD:TSOT-23-5", ""])
    check(sel2 and sel2[0] == "X2SON, SOT-23",
          "scoped: comma-listed label matched against the board footprint",
          str(sel2 and sel2[0]))


def test_overextraction_is_refused():
    """More pins than the part HAS means tables were merged. That is a wrong
    table wearing the appearance of a complete one, so it is refused."""
    doc = "\n".join(f"  P{i}  {i}  I/O  desc" for i in range(1, 10))
    r = X.extract(doc, pages_wanted=[1], pin_count=5)
    check(r["overextracted"] == 9, "overextraction: detected", str(r["overextracted"]))
    check(r["pins"] == {}, "overextraction: no pins returned")
    check(r["coverage"]["found"] == 0, "overextraction: reported as zero found")


def test_extract_name_plus_numlist():
    """One line owning many pins, with no type column:
         RESERVED    18, 43, 55, 73-84, 113, 114, 116, 117, 140-144
    That single row is 20+ pins the typed patterns cannot see."""
    line = ("  RESERVED        18, 43, 55, 73-84, 113, 114, 116, 117, 140-144\n")
    rows = X.recognise_rows(line, 1)
    pins = {r[0] for r in rows}
    check("18" in pins and "43" in pins, "numlist: singles captured")
    check("73" in pins and "84" in pins and "80" in pins,
          "numlist: range 73-84 expanded")
    check("144" in pins, "numlist: trailing range captured")
    check(all(r[1] == "RESERVED" for r in rows), "numlist: name applied to all")


def test_range_expansion_is_safe():
    check(X.expand_nums("59, 60") == ["59", "60"], "expand: comma list")
    check(X.expand_nums("85-112")[0] == "85"
          and X.expand_nums("85-112")[-1] == "112"
          and len(X.expand_nums("85-112")) == 28, "expand: inclusive range")
    check(X.expand_nums("112-85") == [], "expand: refuses a reversed range")
    check(X.expand_nums("1-9999") == [], "expand: refuses an absurd span")
    check(X.expand_nums("A1") == ["A1"], "expand: keeps BGA-style pins")


def test_prose_ranges_are_recovered():
    """~40 of 146 pins on a real module exist ONLY in a footnote."""
    prose = ("GND pins 85-112 should be connected to ground in the design. "
             "RESERVED pins 73-84 should not be designed in schematic.")
    found = X.recognise_prose(prose, 1)
    names = {p: n for p, n, _, _ in found}
    check(names.get("85") == "GND" and names.get("112") == "GND",
          "prose: GND range recovered")
    check(names.get("73") == "RESERVED" and names.get("84") == "RESERVED",
          "prose: RESERVED range recovered")
    check(len(found) == 40, "prose: exactly 40 pins recovered", f"got {len(found)}")


def test_series_document_vs_wrong_family():
    """A series doc naming only the family root is family-only; a doc for a
    NEIGHBOURING part is still wrong-part. The difference is whole-token."""
    ec21 = "EC21 Series Hardware Design. The EC21 module supports LTE. " * 30
    check(D.verify("EC21EUXGA-N06-SNNSS", ec21, "")["verdict"] == "family-only",
          "series doc for EC21 -> family-only")
    check(D.verify("EG915NEAAC-N05-SNNSA", ec21, "")["verdict"] == "wrong-part",
          "EC21 doc for an EG915N part -> still wrong-part")
    adsb = "ADS114S08B precision ADC. " * 60
    check(D.verify("ADS114S08IPBSR", adsb, "")["verdict"] == "wrong-part",
          "B-variant doc does NOT become family-only for the non-B part")


def test_incomplete_reference_is_detected():
    """A partial transcription must not look like a completed check.

    Quectel-style module tables are name-major (one row -> several pins) and put
    whole ranges in prose footnotes ("GND pins 85-112"), so a table parse of a
    146-pin module silently misses ~40 pins. Comparing against that would emit
    40 bogus pin_extra findings and still read as "checked"."""
    partial = P.Variant("doc", {"package": "LGA-126", "mpns": ["X"],
                                "pin_count": 126,
                                "pins": {str(i): f"P{i}" for i in range(1, 87)}})
    pins = [N.Pin(str(i), f"P{i}", "passive") for i in range(1, 127)]
    f = P.compare(partial, pins)
    check(len(f) == 1 and f[0]["code"] == "reference_incomplete",
          "partial reference reports reference_incomplete, not 40 pin_extra",
          f"got {[x['code'] for x in f][:5]}")

    # ...but the pins it does have are still compared. Returning early made
    # `report` print OK for eight parts whose names were never looked at.
    wrong = [N.Pin(p.num, "SWAPPED" if p.num == "5" else p.name, p.type)
             for p in pins]
    codes = [x["code"] for x in P.compare(partial, wrong)]
    check("pin_name_mismatch" in codes and "reference_incomplete" in codes,
          "a partial table still catches a wrong name on a pin it covers",
          f"got {codes}")

    complete = P.Variant("doc", {"package": "LGA-126", "mpns": ["X"],
                                 "pin_count": 126,
                                 "pins": {str(i): f"P{i}" for i in range(1, 127)}})
    check(not P.compare(complete, pins), "complete reference compares cleanly")


def test_pinmapped_discriminator():
    """Passives must stay out of the system with no manual classification."""
    lp = N.LibPart("Device", "R", [N.Pin("1", "~", "passive"),
                                   N.Pin("2", "~", "passive")], [], {})
    check(not lp.pinmapped(), "2-pin unnamed resistor is NOT pin-mapped")
    lp = N.LibPart("X", "U", [N.Pin("1", "VDD", "power_in"),
                              N.Pin("2", "GND", "power_in")], [], {})
    check(lp.pinmapped(), "2-pin part with functional names IS pin-mapped")
    lp = N.LibPart("X", "U", [N.Pin(str(i), "~", "passive") for i in range(1, 4)],
                   [], {})
    check(lp.pinmapped(), "3-pin part is pin-mapped even when unnamed")


def test_wizard_cannot_bless_a_wrong_symbol():
    """Walk the TMUX incident through the wizard with scripted answers.

    The person types the datasheet's table and says "the symbol is wrong" for
    the differences. The table ends up signed and checked, the symbol fix is
    noted, and the line stays OPEN: only an explicit "same pin" answer could
    close it, and that answer is never the default."""
    import shutil
    import tempfile
    import todo as TD
    import wizard as WZ
    comps, _ = N.parse(os.path.join(FIX, "tmux_broken.xml"))
    att = os.path.join(tempfile.mkdtemp(), "attestations")
    draft = os.path.join(att, "reference", "draft")
    os.makedirs(draft)
    with open(os.path.join(draft, "STUB-TMUX1309APWR.toml"), "w") as f:
        f.write('doc_id = ""\norigin = "manual"\nextracted_by = ""\n\n'
                '  [[variant]]\n  package = "TSSOP-16"\n'
                '  mpns = ["TMUX1309APWR"]\n  pin_count = 16\n\n'
                '    [variant.pins]\n'
                + "".join(f'    {n} = ""\n' for n in range(1, 17)))
    table = ["S0B", "S2B", "DB", "S3B", "S1B", "EN", "NC", "GND",
             "A1", "A0", "S3A", "S0A", "DA", "S1A", "S2A", "VDD"]

    lines, items = TD.collect(comps, att)
    check([i.kind for i in items] == ["stub_unfilled"],
          "wizard: the stub is the open item", str(items))

    def answer(prompt):
        import re
        m = re.match(r"\s*pin (\d+):", prompt)
        return (table[int(m.group(1)) - 1] if m else
                "Tester" if prompt.startswith("Your name") else
                "1" if "[a/1]" in prompt else
                "y" if "[a/t/y" in prompt else ":q")
    said = []
    WZ._find_pdf = lambda *a: ""        # offline: no datasheet lookups
    WZ.Wizard(att, att, lines, items, ask=answer, say=said.append,
              today="2026-09-30", auto_open=False).run()

    signed = os.path.join(att, "reference", "datasheet", "TMUX1309APWR.toml")
    check(os.path.isfile(signed), "wizard: the typed table is signed and moved",
          "\n".join(said[-5:]))
    if os.path.isfile(signed):
        doc = P.Document(signed, tomlite.load(signed))
        check(doc.reviewed_by == "Tester" and doc.reviewed_on == "2026-09-30",
              "wizard: the signature names the person")
    check(not os.path.isfile(os.path.join(att, "waivers.toml")),
          "wizard: 'the symbol is wrong' records no waiver")
    _l, after = TD.collect(comps, att)
    check("pin_difference" in [i.kind for i in after],
          "wizard: the wrong symbol still fails after the session",
          str(after))
    shutil.rmtree(os.path.dirname(att))


def test_login_gated_datasheet_is_filed_under_its_url():
    """Quectel serves datasheets behind a login. The schematic keeps the page
    URL; a PDF downloaded by hand is filed under it, and fetching that URL then
    works offline. The .invalid host guarantees no network is involved."""
    import shutil
    import tempfile
    cache = tempfile.mkdtemp()
    pdf = os.path.join(cache, "doc.pdf")
    with open(pdf, "wb") as f:
        f.write(b"%PDF-1.4\n%%EOF\n")
    url = "https://login.example.invalid/download/part"
    check(D.add_local_copy(url, pdf, cache) == "", "add-pdf: accepts a PDF")
    got = D.fetch("PART", "symbol-field", url, cache)
    check(got.ok and got.url == url, "add-pdf: the URL now resolves offline",
          got.error)
    with open(pdf, "wb") as f:
        f.write(b"<html>login</html>")
    check(D.add_local_copy(url, pdf, cache) != "", "add-pdf: refuses a non-PDF")
    shutil.rmtree(cache)


def test_datasheet_rule_and_exemptions():
    """Every line needs a datasheet: a URL, a project file, an embedded file,
    or a signed exemption. A path to a file that is not there does not count."""
    import shutil
    import tempfile
    import exempt as EX
    import todo as TD

    class Comp:
        def __init__(self, ds):
            self.ds = ds
        mpn_effective = ("PART", "mpn-field")

        def field(self, name):
            return self.ds if name == "Datasheet" else ""

    root = tempfile.mkdtemp()
    proj, att = os.path.join(root, "board"), os.path.join(root, "attestations")
    os.makedirs(os.path.join(proj, "datasheets"))
    open(os.path.join(proj, "datasheets", "p.pdf"), "wb").write(b"%PDF-1.4\n")
    state = lambda ds: TD.datasheet_state(Comp(ds), proj, EX.load(att))[0]

    check(state("https://x.example/p.pdf") == "ok", "rule: a URL counts")
    check(state("${KIPRJMOD}/datasheets/p.pdf") == "ok", "rule: a project file counts")
    check(state("kicad-embed://p.pdf") == "ok", "rule: an embedded file counts")
    check(state("${KIPRJMOD}/datasheets/gone.pdf") == "missing",
          "rule: a path to a missing file does not count")
    check(state("~") == "missing", "rule: an empty field does not count")
    check(state(os.path.join(proj, "datasheets", "p.pdf")) == "missing",
          "rule: an absolute path does not count, even when the file exists")

    EX.add(att, "PART", "confidential", "", "", "2026-09-30")
    check(state("") == "missing", "rule: an exemption nobody signed does not count")
    shutil.rmtree(att)
    EX.add(att, "PART", "none_exists", "drawing only", "Tester", "2026-09-30")
    check(state("") == "exempt", "rule: a signed exemption counts")
    check(EX.load(att)["PART"].note == "drawing only", "exemptions: note round-trips")

    # The wizard links a file inside the project relative to it, even when
    # the project is not in a git repo.
    import wizard as WZ
    import decisions as DC

    class Line:
        mpn, refs = "PART", ["U1"]
        comp = Comp("")
    w = WZ.Wizard(att, proj, [], [], ask=lambda p: ":q", say=lambda t: None,
                  auto_open=False)
    pdf = os.path.join(proj, "datasheets", "p.pdf")
    w.accept_pdf = lambda *a: True
    w.datasheet_file(TD.Item("datasheet_missing", "PART", ["U1"]), Line(), pdf)
    edits = DC.parse_fields(w.fields_csv)
    check([e.value for e in edits] == ["${KIPRJMOD}/datasheets/p.pdf"],
          "wizard: a project file is linked relative to the project",
          str([e.value for e in edits]))

    # A datasheet given earlier, not yet in the schematic, is the one shown.
    known = w.known_pdf(Line())
    check(bool(known) and open(known, "rb").read() == open(pdf, "rb").read(),
          "wizard: shows the datasheet an answer gave, before apply", known)

    # A datasheet already on screen is the one the question asks about.
    asked = []
    w.buttons = True
    w.session_pdf["PART"] = pdf
    w.confirm_guess = lambda it, ln, path: asked.append(path) or "y"
    w.do_datasheet_missing(TD.Item("datasheet_missing", "PART", ["U1"]), Line())
    check(asked == [os.path.abspath(pdf)],
          "wizard: asks about the datasheet the pane shows", str(asked))
    shutil.rmtree(root)


def test_active_low_spellings():
    """~{RESET} in KiCad is RESET_N in a datasheet: the same pin, not a
    difference. A symbol that drops the polarity is still caught."""
    def codes(sym, ds):
        v = P.Variant("d", {"package": "P", "mpns": ["M"], "pins": {"1": ds}})
        return [f["code"] for f in P.compare(v, [N.Pin("1", sym, "input")])]
    for sym, ds in (("~{RESET}", "RESET_N"), ("~{CS}", "CS_B"), ("/WP", "WP_N"),
                    ("~{RESET}", "RESETn"), ("~{EN}", "EN_L"),
                    ("MISO/~{DRDY}", "MISO/DRDY_N")):
        check(codes(sym, ds) == ["pin_name_convention"],
              f"active-low: {sym} is {ds}", str(codes(sym, ds)))
    for sym, ds in (("RESET", "RESET_N"), ("~{E}", "EN"), ("~{RESET}", "RST_N"),
                    ("~{RESET}", "RESET_P")):
        check("pin_name_mismatch" in codes(sym, ds),
              f"active-low: {sym} vs {ds} is still a difference", str(codes(sym, ds)))
    check(codes("REF0_N", "REFN0") == ["pin_name_convention"],
          "active-low: the differential rule still applies")


def test_wizard_opens_the_pinout_page():
    """The page the wizard opens: pin names win, contents pages never do."""
    import wizard as WZ
    toc = "\n".join(f"Pin configuration {'. ' * 20} {n}" for n in range(3, 12))
    text = "\f".join([
        "Front page",
        toc,
        "Features and ordering",
        "Pin configuration and functions\n1 VIN\n2 GND",
        "Pin table\n1 VDD  2 GND  3 SCLK  4 DIN  5 DOUT  6 CS",
        "Package outline SOIC",
    ])
    check(WZ._best_page(text) == 4, "page: the pin heading, not the contents",
          str(WZ._best_page(text)))
    check(WZ._best_page(text, ["SCLK", "DIN", "DOUT", "CS"]) == 5,
          "page: the page holding the table's own names wins")
    check(WZ._package_word("Package_SO:SOIC-8_5.23x5.23mm") == "SOIC",
          "page: package word from the footprint")


def _fixture(name):
    path = os.path.join(HERE, "fixtures", name)
    return open(path, encoding="utf-8").read() if os.path.isfile(path) else None


def test_register_table_is_not_a_pin_table():
    """S25FL064L has no pin table at all, only connection drawings, and a
    status register whose bit rows (7 SRP0_NV, 6 SEC_NV, ...) have the shape
    of pin rows. The register used to BE the pin table: pins 1-7 all wrong.
    Now the drawing is read, and the SOIC-16 drawing is ignored for a SOIC-8."""
    text = _fixture("s25fl064l_pinout.txt")
    if text is None:
        check(True, "s25: skipped, no fixture")
        return
    ctx = T.Context(mpn="S25FL064LABMFI013",
                    footprint="Package_SO:SOIC-8_5.3x5.3mm_P1.27mm",
                    expect_pins=8)
    out = T.read_pin_table(text, ctx)
    want = {"1": "CS#", "2": "SO/IO1", "3": "WP#/IO2", "4": "VSS",
            "5": "SI/IO0", "6": "SCK", "7": "IO3/RESET#", "8": "VCC"}
    check(out["pins"] == want, "s25: the SOIC-8 drawing is the pin table",
          str(out["pins"]))
    check(not any("_NV" in v for v in out["pins"].values()),
          "s25: no register field names")


def test_pinout_drawing_needs_the_ladder():
    """A drawing row is two numbers between two names. Only a complete ladder
    whose rows sum to N+1 counts; a stray "A 3  6 B" line does not."""
    good = ("   VDD   1        4   OUT\n\n   GND   2        3   IN\n")
    r = T.shape_pinout_diagram(good, T.Context(expect_pins=4))
    check(r.pins == {"1": "VDD", "4": "OUT", "2": "GND", "3": "IN"},
          "drawing: a two-row ladder reads", str(r.pins))
    broken = ("   VDD   1        4   OUT\n\n   GND   3        2   IN\n")
    check(not T.shape_pinout_diagram(broken, T.Context(expect_pins=4)).pins,
          "drawing: rows that do not sum to N+1 are not a drawing")
    check(not T.shape_pinout_diagram(good, T.Context(expect_pins=6)).pins,
          "drawing: a 4-pin drawing is not read for a 6-pin part")


def test_shared_datasheet_scopes_by_device():
    """TPD1E05U06 and TPD4E05U06 share a datasheet and a table heading. The
    wrong table put GND on pin 2 of the four-channel part; the device name in
    the heading is what separates them. The drawing agrees with the table."""
    text = _fixture("tpd4e05u06_pinfn.txt")
    if text is None:
        check(True, "tpd4e05: skipped, no fixture")
        return
    ctx = T.Context(mpn="TPD4E05U06QDQARQ1",
                    footprint="Package_SON:USON-10_2.5x1.0mm_P0.5mm",
                    expect_pins=10)
    out = T.read_pin_table(text, ctx)
    check(out["scope"] == "TPD4E05U06-Q1 DQA", "tpd4e05: scoped to its table",
          str(out["scope"]))
    check(out["pins"].get("2") == "D1-" and out["pins"].get("5") == "D2-",
          "tpd4e05: D1- and D2- read (en dash in the PDF)",
          f"{out['pins'].get('2')} {out['pins'].get('5')}")
    check(out["complete"] and not out["conflicts"],
          "tpd4e05: complete, and table and drawing agree", str(out["conflicts"]))
    n, why = OC.document_pin_count("TPD4E05U06QDQARQ1", text)
    check(n == 10, "tpd4e05: 10 pins from the orderable-parts line", why)


def test_header_table_reads_by_column():
    """Renesas types the D and S pins "MOSFET", which no type pattern knows,
    so pins 3-5 went missing and the drawing's "D 3  6 S" gave pin 6 the name
    D. Reading by the heading's columns gets all eight."""
    text = _fixture("slg59m301v_pins.txt")
    if text is None:
        check(True, "slg: skipped, no fixture")
        return
    ctx = T.Context(mpn="SLG59M301V", footprint="Jitter_IC:DFN200X150X80-8N",
                    expect_pins=8)
    r = T.shape_header_table(text, ctx)
    want = {"1": "VDD", "2": "ON", "3": "D", "4": "D", "5": "S", "6": "S",
            "7": "CAP", "8": "GND"}
    check(r.pins == want, "slg: heading columns read all eight", str(r.pins))
    out = T.read_pin_table(text, ctx)
    check(out["pins"] == want and not out["conflicts"],
          "slg: driver agrees, drawing adds no conflict", str(out["conflicts"]))


def test_exposed_pad_is_numbered_from_the_footprint():
    """TPS1H200A lists its thermal pad with no number. The footprint
    (VSSOP-8-1EP) says there is exactly one, which KiCad numbers 9."""
    text = _fixture("tps1h200a_pinfn.txt")
    if text is None:
        check(True, "ep: skipped, no fixture")
        return
    n, why = OC.document_pin_count("TPS1H200AQDGNRQ1", text)
    check(n == 8, "ep: 8 leads from the orderable-parts line", f"{n} {why}")
    fp = "Jitter_IC:Texas_DGN0008K_VSSOP-8-1EP_3x3mm_P0.65mm"
    out = T.read_pin_table(text, T.Context(mpn="TPS1H200AQDGNRQ1",
                                           footprint=fp, expect_pins=8))
    check(out["pins"].get("9") == "Thermal pad" and out["complete"],
          "ep: the pad is pin 9 and the table is complete", str(out["pins"]))
    check("9" in out["low_confidence"], "ep: ...marked for review")
    bare = T.read_pin_table(text, T.Context(mpn="TPS1H200AQDGNRQ1",
                                            footprint="VSSOP-8", expect_pins=8))
    check("9" not in bare["pins"],
          "ep: without an exposed pad in the footprint, no pin 9 is invented")


def test_quectel_sections_are_one_table():
    """Quectel cuts its pin table into sections that each repeat a heading
    stacked over three lines, continues rows over page breaks, and wraps the
    RESERVED list above and below its name. All of it is one table."""
    text = _fixture("eg915n_pindesc.txt")
    if text is None:
        check(True, "quectel: skipped, no fixture")
        return
    r = T.shape_header_table(text, T.Context(mpn="EG915NEAAC-N05-SNNSA",
                                             expect_pins=126))
    check(r.pins.get("52") == "VBAT_RF",
          "quectel: a row after the page break still reads", str(r.pins.get("52")))
    check(r.pins.get("3") == "GND" and r.pins.get("102") == "GND",
          "quectel: the GND list reads to its end")
    check(r.pins.get("11") == "RESERVED" and r.pins.get("125") == "RESERVED",
          "quectel: the RESERVED list wrapped around its name reads whole",
          f"{r.pins.get('11')} {r.pins.get('125')}")
    check(r.pins.get("83") in (None, "USIM2_DET"),
          "quectel: a footnote '*' is not part of a name")


def test_st_names_centred_on_their_row():
    """ST centres a long name on its row: PH0-OSC_IN (PH0) prints "PH0-",
    then "OSC_I" on the row line, then "N (PH0)". The row's own line holds
    only the middle, which used to BE the name. PA14 (JTCK/SWCLK) is the first
    row on its page, so half its name sits above the first row. PB11's row
    has "NC" in another package's column, which dropped the whole row."""
    text = _fixture("stm32_p81_p92_p106.txt")
    if text is None:
        check(True, "st-centred: skipped, no fixture")
        return
    pins, meta = X.extract_multicolumn(text, "STM32L4R5", "LQFP100")
    check(pins.get("12") == "PH0-OSC_IN (PH0)", "st-centred: pin 12 whole",
          repr(pins.get("12")))
    check(pins.get("13") == "PH1-OSC_OUT (PH1)",
          "st-centred: pin 13 does not take pin 12's (PH0)", repr(pins.get("13")))
    check(pins.get("14") == "NRST", "st-centred: pin 14 keeps its own name")
    check(pins.get("48") == "PB11", "st-centred: an NC cell is not a broken row")
    check(pins.get("76") == "PA14 (JTCK/SWCLK)",
          "st-centred: the first row's name starts above it", repr(pins.get("76")))


def test_pin_count_from_the_document():
    """The expected count decides whether a partial read looks complete.
    It comes from the datasheet's own words, never from the symbol."""
    ti = ("  TPS7A2033PDBVR        Active    Production     SOT-23 (DBV) | 5"
          "    3000 | LARGE T&R        Yes\n"
          "   TPS7A2033PDBVR     SOT-23   DBV     5     3000      178.0\n")
    check(OC.document_pin_count("TPS7A2033PDBVR", ti)[0] == 5,
          "count: TI orderable-parts lines")
    check(OC.document_pin_count("TPS7A2033PDBVR", ti.replace("| 5", "| 6"))[0]
          == 0, "count: two different answers give none")
    nx = "BC857BS          TSSOP6       Plastic surface-mounted package; 6 leads\n"
    check(OC.document_pin_count("BC857BS", nx)[0] == 6,
          "count: a package line naming its leads")
    mc = "Note: Applies to 5-Lead SC70.\nNote: Applies to 3-Lead SOT-23.\n"
    check(OC.document_pin_count("MCP9701AT-E/LT", mc,
                                "Package_TO_SOT_SMD:SOT-353_SC-70-5")[0] == 5,
          "count: the package description matching the footprint")
    text = _fixture("sic473_pinconfig.txt")
    if text is not None:
        check(OC.drawing_pin_count(text)[0] == 30,
              "count: SiC473's drawing labels pads 1-30 (27 leads + 3 pads)",
              str(OC.drawing_pin_count(text)))


def test_decimal_comma_is_not_a_pin_list():
    """A Wuerth LED datasheet gave pins 0 and 8 the name R0 from "R0  0,8",
    a radius of 0.8 mm."""
    check(X.expand_nums("0,8") == [], "decimal comma: not pins", str(X.expand_nums("0,8")))
    check(X.expand_nums("6, 9") == ["6", "9"], "decimal comma: a real list still reads")
    ctx = T.Context(mpn="150060VS75000")
    out = T.read_pin_table("   R0                  0,8\n", ctx)
    check(out["pins"] == {}, "decimal comma: nothing extracted", str(out["pins"]))


def test_one_impossible_pin_discards_the_reading():
    """A reading that names pin 15 of an 8-pin part has read some other table
    or a footer, so its other rows go too. Better nothing than wrong. A good
    reading beside it is kept."""
    ctx = T.Context(mpn="X", expect_pins=8)

    def stray(text, c):
        return T.Result("row_per_pin", {"1": "VDD", "2": "ON", "15": "CFR0011"})

    out = T.read_pin_table("x", ctx, shapes=(stray,))
    check(out["pins"] == {}, "impossible pin: the whole reading is dropped",
          str(out["pins"]))
    check(any("discarded" in n for n in out["notes"]),
          "impossible pin: and it says so", str(out["notes"]))

    def good(text, c):
        return T.Result("header_table", {str(i): f"P{i}" for i in range(1, 9)})

    out2 = T.read_pin_table("x", ctx, shapes=(good, stray))
    check(len(out2["pins"]) == 8 and "15" not in out2["pins"]
          and out2["pins"]["1"] == "P1",
          "impossible pin: a good reading beside it is kept", str(out2["pins"]))


def test_ground_and_spi_synonyms():
    """GND is VSS everywhere. CLK/SCK, DO/SO/MISO and DI/SI/MOSI are the same
    pin only on a part whose SPI role the datasheet established; elsewhere a
    human decides."""
    def codes(sym, ds, role=""):
        v = P.Variant("d", {"package": "P", "mpns": ["M"], "pins": {"1": ds},
                            "bus_role": role})
        return [f["code"] for f in P.compare(v, [N.Pin("1", sym, "input")])]
    ok = ("pin_name_convention", "pin_name_partial")
    check(codes("GND", "VSS") == ["pin_name_convention"], "synonym: GND is VSS")
    for sym, ds in (("CLK", "SCK"), ("DO", "SO"), ("DI", "SDI"), ("MISO", "DOUT"),
                    ("COPI", "SI"), ("~{CS}", "~{SS}"), ("DO", "SO/IO1"),
                    ("DI", "SI/IO0")):
        got = codes(sym, ds, "peripheral")
        check(len(got) == 1 and got[0] in ok, f"synonym: {sym} is {ds} on a peripheral",
              str(got))
        check("pin_name_mismatch" in codes(sym, ds) or
              "pin_name_role_unknown" in codes(sym, ds),
              f"synonym: {sym} vs {ds} needs a human when the role is unknown",
              str(codes(sym, ds)))
    check("pin_name_mismatch" in codes("MISO", "DIN", "peripheral"),
          "synonym: MISO on a peripheral's DIN is still wrong")
    for board, dev in (("PICO", "SDI"), ("POCI", "SDO"), ("COPI", "PICO"),
                       ("CIPO", "POCI"), ("MISO", "POCI")):
        check(codes(board, dev, "peripheral")[0] in ok,
              f"synonym: {board} is {dev} on a peripheral")
    for board, dev in (("POCI", "SDI"), ("PICO", "SDO"), ("POCI", "MOSI"),
                       ("PICO", "CIPO")):
        check("pin_name_mismatch" in codes(board, dev, "peripheral"),
              f"synonym: {board} vs {dev} is backwards and caught")
    check("pin_name_mismatch" in codes("DO", "SI", "peripheral"),
          "synonym: data out is not data in")
    check("pin_name_mismatch" in codes("~{HOLD}", "IO3/RESET#", "peripheral"),
          "synonym: HOLD is not RESET")
    check(P.Variant("d", {"bus_role": "slave"}).bus_role == "peripheral",
          "synonym: older files saying slave still work")


def test_wizard_exits_cleanly_and_pin_names_are_free():
    """Commands carry a colon, so q, s and o are ordinary answers (a FET has a
    pin called S). Ctrl-C at a prompt stops like :q, with nothing lost."""
    import tempfile
    import todo as TD
    import wizard as WZ
    comps, _ = N.parse(os.path.join(FIX, "tmux_broken.xml"))
    att = os.path.join(tempfile.mkdtemp(), "attestations")
    draft = os.path.join(att, "reference", "draft")
    os.makedirs(draft)
    stub = os.path.join(draft, "STUB-TMUX1309APWR.toml")
    with open(stub, "w") as f:
        f.write('doc_id = ""\norigin = "manual"\nextracted_by = ""\n\n'
                '  [[variant]]\n  package = "TSSOP-16"\n'
                '  mpns = ["TMUX1309APWR"]\n  pin_count = 16\n\n'
                '    [variant.pins]\n'
                + "".join(f'    {n} = ""\n' for n in range(1, 17)))
    WZ._find_pdf = lambda *a: ""
    answers = iter(["Tester", "s", "q", "o"])

    def ask(prompt):
        try:
            return next(answers)
        except StopIteration:
            raise KeyboardInterrupt
    said = []
    lines, items = TD.collect(comps, att)
    WZ.Wizard(att, att, lines, items, ask=ask, say=said.append,
              auto_open=False).run()
    text = open(stub).read()
    check('1 = "s"' in text and '2 = "q"' in text and '3 = "o"' in text,
          "wizard: s, q and o are ordinary pin names", text[-200:])
    check(any("Stopped" in x for x in said), "wizard: Ctrl-C stops cleanly",
          said[-1] if said else "")
    tomlite.load(stub)

    # In a multiple-choice prompt the bare letter is a command again.
    w = WZ.Wizard(att, att, [], [], ask=lambda p: "s", say=lambda t: None,
                  auto_open=False)
    try:
        w.ask("sign", ["y", "n"])
        check(False, "wizard: bare s skips in a y/n prompt")
    except WZ.Skip:
        pass
    w.ask_raw = lambda p: "s"
    check(w.ask("pick", ["s", "x"]) == "s",
          "wizard: a listed choice s is an answer, not a command")


def test_wizard_back_undoes_the_part():
    """:b undoes the answers given for this part and asks them again, so an
    answer given too fast can be changed."""
    import tempfile
    import todo as TD
    import wizard as WZ
    comps, _ = N.parse(os.path.join(FIX, "tmux_broken.xml"))
    att = os.path.join(tempfile.mkdtemp(), "attestations")
    draft = os.path.join(att, "reference", "draft")
    os.makedirs(draft)
    stub = os.path.join(draft, "STUB-TMUX1309APWR.toml")
    with open(stub, "w") as f:
        f.write('doc_id = ""\norigin = "manual"\nextracted_by = ""\n\n'
                '  [[variant]]\n  package = "TSSOP-16"\n'
                '  mpns = ["TMUX1309APWR"]\n  pin_count = 16\n\n'
                '    [variant.pins]\n'
                + "".join(f'    {n} = ""\n' for n in range(1, 17)))
    WZ._find_pdf = lambda *a: ""
    answers = iter(["Tester", "A", "B", ":b", "X"])

    def ask(prompt):
        try:
            return next(answers)
        except StopIteration:
            raise KeyboardInterrupt
    said = []
    lines, items = TD.collect(comps, att)
    WZ.Wizard(att, att, lines, items, ask=ask, say=said.append,
              auto_open=False).run()
    text = open(stub).read()
    check('1 = "X"' in text and '2 = ""' in text,
          "wizard: back undoes this part's answers and asks again", text[-200:])
    check(any("Back:" in x for x in said), "wizard: back says what it undid")


def test_tables_in_the_component_library():
    """Signed tables are read from the board's repo and from a component
    library: a git repo, other than the board's, that holds a symbol library
    the project uses. The board's own table for an MPN wins. The wizard saves
    a library part's table in the library, a project-local one in the board."""
    import shutil
    import subprocess
    import tempfile
    import library as LIB
    import todo as TD
    import wizard as WZ
    top = tempfile.mkdtemp()
    board_repo, lib = os.path.join(top, "hw"), os.path.join(top, "hw", "libs", "Comp")
    proj = os.path.join(board_repo, "board")
    for d in (proj, lib, os.path.join(board_repo, "local")):
        os.makedirs(d)
    for d in (board_repo, lib):
        subprocess.run(["git", "init", "-q", d], check=True)
    open(os.path.join(lib, "Comp.kicad_sym"), "w").write("(kicad_symbol_lib)")
    open(os.path.join(board_repo, "local", "Own.kicad_sym"), "w").write("(kicad_symbol_lib)")
    open(os.path.join(proj, "sym-lib-table"), "w").write(
        '(sym_lib_table\n'
        '  (lib (name "Comp")(type "KiCad")(uri "${KIPRJMOD}/../libs/Comp/Comp.kicad_sym")(options "")(descr ""))\n'
        '  (lib (name "Own")(type "KiCad")(uri "${KIPRJMOD}/../local/Own.kicad_sym")(options "")(descr ""))\n'
        '  (lib (name "Sys")(type "KiCad")(uri "${KICAD9_SYMBOL_DIR}/Device.kicad_sym")(options "")(descr ""))\n)')
    check(LIB.libraries(proj) == [os.path.realpath(lib)],
          "library: found from the sym-lib-table, the board's own repo is not one",
          str(LIB.libraries(proj)))
    check([LIB.scope_of(x, proj) for x in ("Comp:U", "Own:U", "Device:R")]
          == ["library", "project", "library"],
          "library: a project-local symbol's table stays in the project")

    def table(dirpath, name, pin1):
        os.makedirs(dirpath, exist_ok=True)
        open(os.path.join(dirpath, name + ".toml"), "w").write(
            f'doc_id = "d"\nreviewed_by = "T"\n\n  [[variant]]\n  mpns = ["{name}"]\n'
            f'  pin_count = 1\n\n    [variant.pins]\n    1 = "{pin1}"\n')
    att = os.path.join(board_repo, "attestations")
    table(LIB.signed_dir(lib), "PART", "LIB")
    table(LIB.signed_dir(lib), "OTHER", "X")
    table(os.path.join(att, "reference", "datasheet"), "PART", "OWN")
    docs = TD.signed_docs(att, proj)
    pins = {v.mpns[0]: v.pins["1"] for _f, d in docs for v in d.variants}
    check(pins == {"PART": "OWN", "OTHER": "X"},
          "library: tables read from both, the board's own wins", str(pins))
    check(len(LIB.uncommitted(os.path.realpath(lib))) == 2,
          "library: new tables there are reported as not committed")

    class Comp:
        libsource = "Comp:U"
    class Line:
        mpn, refs, comp = "PART", ["U1"], Comp()
    w = WZ.Wizard(att, proj, [], [], ask=lambda p: "", say=lambda t: None,
                  auto_open=False)
    check(w.save_dir(Line()) == LIB.signed_dir(os.path.realpath(lib)),
          "wizard: a library part's table is proposed for the library")
    Line.comp = type("C", (), {"libsource": "Own:U"})()
    check(w.save_dir(Line()) == os.path.join(att, "reference", "datasheet"),
          "wizard: a project-local part's table is proposed for the project")
    w.save_to = "library"
    check(w.save_dir(Line()) == LIB.signed_dir(os.path.realpath(lib)),
          "wizard: --save-to library overrides the proposal")
    shutil.rmtree(top)


def test_review_folder_stays_out_of_git():
    """review/ is regenerated: bomcheck puts a .gitignore in it, so a board
    repo commits only what matters."""
    import shutil
    import tempfile
    import cli
    top = tempfile.mkdtemp()
    proj = os.path.join(top, "board")
    review = os.path.join(top, "attestations", "review")
    os.makedirs(proj)
    os.makedirs(review)
    cli.ignore_review(proj)
    text = open(os.path.join(review, ".gitignore")).read()
    check("*" in text.split(), "review/: ignored by its own .gitignore", text)
    shutil.rmtree(top)


def test_apply_ignores_other_projects_designators():
    """A symbol copied from another project keeps that project's (instances)
    entry. Its old designator must not make apply edit this symbol. A value
    with a quote must pass the read-back check."""
    import shutil
    import tempfile
    import schematic as SCH
    d = tempfile.mkdtemp()
    open(os.path.join(d, "board.kicad_pro"), "w").write("{}")

    def sym(ref, other):
        return (f'  (symbol (lib_id "L:X")\n'
                f'    (property "Reference" "{ref}")\n'
                f'    (property "MPN" "")\n'
                f'    (instances\n'
                f'      (project "board" (path "/a" (reference "{ref}") (unit 1)))\n'
                f'      (project "elsewhere" (path "/b" (reference "{other}") (unit 1)))))\n')
    sch = os.path.join(d, "board.kicad_sch")
    open(sch, "w").write("(kicad_sch\n" + sym("U7", "U4") + sym("U4", "U9") + ")\n")
    insts = {i.ref: i.refs for i in SCH.instances(sch)}
    check(insts == {"U7": ["U7"], "U4": ["U4"]},
          "schematic: designators of another project are not this symbol's", str(insts))
    applied, problems = SCH.apply_edits([("U4", "MPN", 'A"B', SCH.find(d, "U4"))],
                                        allow_dirty=True)
    text = open(sch).read()
    u7 = text[text.index('"U7"'):text.index('"U4"')]
    check(applied and not problems and 'A\\"B' in text and 'A\\"B' not in u7,
          "schematic: apply edits U4 itself, and a quote survives the read-back",
          str(problems))
    shutil.rmtree(d)


def test_design_sheets_only():
    """Only the root sheet and the sheets it uses are the design. An old or
    variant schematic in the same folder, with the same designators, is not:
    apply must not write into it. A relative project path resolves once."""
    import shutil
    import tempfile
    import schematic as SCH
    import datasheet as D
    d = tempfile.mkdtemp()
    open(os.path.join(d, "board.kicad_pro"), "w").write("{}")
    open(os.path.join(d, "board.kicad_sch"), "w").write(
        '(kicad_sch (sheet (property "Sheetfile" "sub.kicad_sch")))')
    open(os.path.join(d, "sub.kicad_sch"), "w").write("(kicad_sch)")
    open(os.path.join(d, "old_variant.kicad_sch"), "w").write("(kicad_sch)")
    got = [os.path.basename(f) for f in SCH.sheets(d)]
    check(got == ["board.kicad_sch", "sub.kicad_sch"],
          "schematic: only the sheets the design uses", str(got))
    cwd = os.getcwd()
    os.chdir(os.path.dirname(d))
    try:
        rel = D.field_path("${KIPRJMOD}/datasheets/x.pdf", os.path.basename(d))
    finally:
        os.chdir(cwd)
    check(rel == os.path.join(d, "datasheets", "x.pdf"),
          "datasheet: a relative project path is not joined twice", rel)
    shutil.rmtree(d)


def test_wizard_asks_again_what_is_still_open():
    """A part still open after its answers is asked about again, not
    reported as finished. A skipped part is not; the summary lists it."""
    import tempfile
    import todo as TD
    import wizard as WZ
    comps, _ = N.parse(os.path.join(FIX, "tmux_broken.xml"))
    att = os.path.join(tempfile.mkdtemp(), "attestations")
    draft = os.path.join(att, "reference", "draft")
    os.makedirs(draft)
    with open(os.path.join(draft, "STUB-TMUX1309APWR.toml"), "w") as f:
        f.write('doc_id = ""\norigin = "manual"\nextracted_by = ""\n\n'
                '  [[variant]]\n  package = "TSSOP-16"\n'
                '  mpns = ["TMUX1309APWR"]\n  pin_count = 16\n\n'
                '    [variant.pins]\n'
                + "".join(f'    {n} = ""\n' for n in range(1, 17)))
    WZ._find_pdf = lambda *a: ""
    answers = iter(["Tester", "A"] + [""] * 15 + [":s"])

    def ask(prompt):
        try:
            return next(answers)
        except StopIteration:
            raise KeyboardInterrupt
    said = []
    lines, items = TD.collect(comps, att)
    w = WZ.Wizard(att, att, lines, items, ask=ask, say=said.append, auto_open=False)
    w.recollect = lambda: TD.collect(comps, att)
    w.run()
    check(any("still have something open" in x for x in said),
          "wizard: a part left open is asked about again", said[-4:])
    s = w.summary()
    check(not s["stopped"] and [o["mpn"] for o in s["open"]] == ["TMUX1309APWR"],
          "wizard: the finish summary lists the skipped part", str(s))


def test_polarity_parts():
    """Two-pin polar parts: the class comes from the symbol's data, the
    footprint's mark from its silk/fab, and the datasheet says what the band
    means. Checked against each other, placement by placement."""
    import footprint as FP
    import polarity as PO
    import todo as TD

    class Lib:
        def __init__(self, desc):
            self.fields = {"Description": desc}

    class Comp:
        def __init__(self, ref, names, desc, sym="Lib:Sym"):
            self.ref, self.libsource = ref, sym
            self.pins = [N.Pin(str(i), n, "passive") for i, n in enumerate(names, 1)]
            self.libpart = Lib(desc)
        mpn_effective = ("M", "mpn-field")

    zener = Comp("D1", ["K", "A"], "Zener diode")
    tvs_bi = Comp("D2", ["A1", "A2"], "Bidirectional TVS diode")
    tvs_uni = Comp("D3", ["A1", "A2"], "400W unidirectional TVS")
    cap = Comp("C1", ["", ""], "Polarized capacitor")
    plain = Comp("C2", ["", ""], "Unpolarized capacitor")
    check([PO.classify(c) for c in (zener, tvs_bi, tvs_uni, cap, plain)] ==
          [PO.POLAR_DIODE, PO.BIDIRECTIONAL, PO.POLAR_DIODE, PO.POLAR_CAP, ""],
          "polarity: class from pin names and the symbol's own description")
    check(PO.marked_pin(tvs_uni, PO.POLAR_DIODE) == "",
          "polarity: a unidirectional symbol with pins A1/A2 names no cathode")

    def fp(marks, tht=False):
        pad = "thru_hole circle" if tht else "smd rect"
        lines = ["(fp_line (start -2 -1) (end 2 -1) (layer \"F.SilkS\"))",
                 "(fp_line (start -2 1) (end 2 1) (layer \"F.SilkS\"))"] + marks
        return FP.Footprint("(footprint \"L:F\" "
                            f"(pad \"1\" {pad} (at -1.5 0)) (pad \"2\" {pad} (at 1.5 0)) "
                            + " ".join(lines) + ")")
    bar1 = '(fp_line (start -2.2 -1) (end -2.2 1) (layer "F.SilkS"))'
    plus2 = ('(fp_line (start 1.2 -2) (end 1.8 -2) (layer "F.SilkS")) '
             '(fp_line (start 1.5 -2.3) (end 1.5 -1.7) (layer "F.SilkS"))')
    check(fp([bar1]).marked_pad()[0] == "1", "footprint: a bar at pad 1 marks pad 1")
    check(fp([plus2]).marked_pad()[0] == "2", "footprint: a + beats the balance")
    check(fp([]).marked_pad()[0] == "", "footprint: a symmetric outline marks nothing")
    check(fp([bar1], tht=True).marked_pad()[0] == "",
          "footprint: through-hole is left to a person")

    class Line:
        mpn, table_path, draft_path = "M", "", ""

        def __init__(self, comps):
            self.comps = comps
            self.refs = [c.ref for c in comps]
            self.comp = comps[0]
    marks = {"D1": ("L:D", "2", "", None), "D2": ("L:D", "1", "", None),
             "C1": ("L:C", "", "no mark", None), "D4": ("L:D", "",
             "confirmed unmarked by a person", None)}
    got = lambda comps: [(i.kind, i.detail) for i in
                         TD.footprint_items(Line(comps), marks)]
    check([k for k, _d in got([zener])] == ["polarity"],
          "footprint: cathode on pin 1, footprint marks pad 2 -> wrong way round")
    check([k for k, _d in got([tvs_bi])] == ["polarity"],
          "footprint: bidirectional symbol on a marked footprint is flagged")
    check([k for k, _d in got([cap])] == ["footprint_mark"],
          "footprint: an unjudged footprint goes to a person")
    unmarked = Comp("D4", ["K", "A"], "Zener diode")
    check("no polarity mark" in got([unmarked])[0][1] if got([unmarked]) else False,
          "footprint: polar part on a footprint confirmed unmarked", str(got([unmarked])))

    hint = PO.datasheet_hint("x\fPolarity   Color band denotes cathode except bipolar\n")
    check(hint[0] == "cathode" and hint[2] == 2, "polarity: hint read from the datasheet",
          str(hint))
    v = P.Variant("d", {"mpns": ["M"], "marked": ""})
    check([f["code"] for f in P.compare(v, zener.pins)] == ["reference_unfilled"],
          "polarity: an empty marking is an unfilled table")
    v = P.Variant("d", {"mpns": ["M"], "marked": "none"})
    check([i.kind for i in TD.datasheet_items(Line([zener]), v)] == ["polarity"],
          "polarity: datasheet says unpolarized, symbol is polar")


def test_waivers():
    """A waiver covers one finding by code, refs and pins, never by message.
    Without `for_rev` it is for good; with it, only while the design is that
    revision. Waivers that cannot apply, expired or match nothing, fail."""
    import shutil
    import tempfile
    import todo as TD
    import waivers as WV
    att = tempfile.mkdtemp()
    mk = lambda kind, w, detail: TD.Item(kind, "", w.refs, "", detail, findings=[w])

    def run(rev, *finding):
        items = [TD.Item(*f[:2], refs=f[2], findings=f[3] if len(f) > 3 else None,
                         code=f[0]) for f in finding]
        return WV.apply(items, WV.load(att), rev, TD.WAIVABLE, mk)

    bidi = ("polarity.bidirectional_on_marked", "M", ["D19"])
    open_, done = run("2.0", bidi)
    check([i.kind for i in open_] == ["polarity.bidirectional_on_marked"],
          "waiver: nothing waived without a waiver")

    WV.add(att, "polarity.bidirectional_on_marked", ["D19"], "harmless", "T", "d",
           until="2.0", about="context only")
    check(WV.load(att)[0].until == "2.0" and WV.load(att)[0].about == "context only",
          "waiver: for_rev and about round-trip")
    open_, done = run("2.0", bidi)
    check(not open_ and len(done) == 1, "waiver: a known issue of rev 2.0 is waived",
          str(open_))
    open_, done = run("3.0", bidi)
    check(sorted(i.kind for i in open_) == ["polarity.bidirectional_on_marked",
                                            "waiver_expired"],
          "waiver: in rev 3.0 the finding fails again, and says why", str(open_))
    open_, done = run("2.0")
    check([i.kind for i in open_] == ["waiver_stale"],
          "waiver: a waiver that matches nothing fails", str(open_))
    WV.renew(att, WV.load(att)[0], "3.0", "T", "d")
    open_, done = run("3.0", bidi)
    check(not open_, "waiver: carried over to rev 3.0 on purpose", str(open_))
    stale = WV.load(att)
    WV.prune(att, stale)
    check(WV.load(att) == [], "waiver: prune removes stale waivers")

    shutil.rmtree(att)
    att = tempfile.mkdtemp()
    two = ("pin_difference", "M", ["U2"], [{"pin": "7"}, {"pin": "8"}])
    WV.add(att, "pin_difference", ["U2"], "rev 3", "T", "d", pins=["7"])
    open_, done = run("2.0", two)
    check(len(open_) == 1 and [f["pin"] for f in open_[0].findings] == ["8"],
          "waiver: waiving pin 7 leaves pin 8 open", str(open_))
    WV.add(att, "unsigned", ["U2"], "no", "T", "d")
    open_, _d = run("2.0", two)
    check("waiver_invalid" in [i.kind for i in open_],
          "waiver: missing work cannot be waived")
    shutil.rmtree(att)


DIODE_XML = """<?xml version='1.0' encoding='utf-8'?>
<export version="E">
  <components>
    <comp ref="D2">
      <value>TVS1</value>
      <footprint>Diode_SMD:D_SOD-123F</footprint>
      <fields><field name="MPN">TVS1</field>
        <field name="Datasheet">https://example.com/tvs1.pdf</field></fields>
      <libsource lib="Device" part="D_TVS" description="Unidirectional TVS diode" />
    </comp>
  </components>
  <libparts>
    <libpart lib="Device" part="D_TVS">
      <description>Unidirectional TVS diode</description>
      <pins><pin num="1" name="K" type="passive" /><pin num="2" name="A" type="passive" /></pins>
    </libpart>
  </libparts>
</export>
"""


def test_half_typed_pin_stub_on_polar_part():
    """A pin stub typed halfway before polarity stubs existed. prepare keeps
    typed data, so status calls it a stub and the wizard offers the switch."""
    import tempfile
    import reffile as R
    import todo as TD
    import wizard as WZ
    tmp = tempfile.mkdtemp()
    xml = os.path.join(tmp, "d.xml")
    with open(xml, "w") as f:
        f.write(DIODE_XML)
    comps, _ = N.parse(xml)
    att = os.path.join(tmp, "attestations")
    draft = os.path.join(att, "reference", "draft")
    os.makedirs(draft)
    stub = os.path.join(draft, R.stub_name("TVS1"))

    def write_stub():
        with open(stub, "w") as f:
            f.write('doc_id = ""\norigin = "manual"\nextracted_by = ""\n\n'
                    '  [[variant]]\n  mpns = ["TVS1"]\n  pin_count = 2\n\n'
                    '    [variant.pins]\n    1 = "q"\n    2 = ""\n')

    write_stub()
    lines, items = TD.collect(comps, att)
    check([i.kind for i in items] == ["polarity_switch"],
          "half stub: one polarity_switch item", str(items))
    rows = TD.table_rows(lines, att)
    check([r.state for r in rows] == ["stub*"], "half stub: status says stub*",
          str([r.state for r in rows]))

    WZ._find_pdf = lambda *a: ""
    checked = os.path.join(att, "reference", "datasheet", "TVS1.toml")
    for answers, want, where in ((["Tester", "y", "b", "c"], 'marked       = "cathode"',
                                  checked),
                                 (["Tester", "n", "K"], '2 = "K"', stub)):
        write_stub()
        lines, items = TD.collect(comps, att)
        feed = iter(answers)
        said = []

        def ask(prompt):
            try:
                return next(feed)
            except StopIteration:
                raise KeyboardInterrupt
        WZ.Wizard(att, att, lines, items, ask=ask, say=said.append,
                  auto_open=False).run()
        text = open(where).read() if os.path.isfile(where) else ""
        check(want in text, f"half stub: answer {answers[1]} -> {want}", text[-300:])
        if where == checked:
            check(not os.path.exists(stub), "half stub: saved as checked, draft gone")
            os.remove(where)
    check('1 = "q"' in text, "half stub: n keeps the typed names")


def test_wizard_closes_its_viewers():
    """Windows opened for a part close when the part is done; one that
    already exited (handed to a window open before) is left alone."""
    import wizard as WZ
    w = WZ.Wizard("/nonexistent", "/nonexistent", [], [], ask=lambda p: "",
                  say=lambda t: None, auto_open=False)
    running = WZ._spawn(["sleep", "30"])
    done = WZ._spawn(["true"])
    done.wait()
    w.viewers = [running, done, None]
    w.close_viewers()
    check(running.wait(timeout=5) is not None and not w.viewers,
          "viewers: closed after the part")
    check(WZ.FP._viewable_size('<svg width="7.0mm" height="5.0mm" viewBox="0 0 7 5">')
          == '<svg width="900" height="643" viewBox="0 0 7 5">',
          "footprint picture: scaled to a viewable size, viewBox kept")
    cropped = WZ.FP._crop('<svg width="100mm" height="80mm" viewBox="0 0 100 80">'
                          '<path d="M 1,2 5,6 Z" /></svg>')
    check('viewBox="0.5000 1.5000 5.0000 5.0000"' in cropped and 'fill="#ffffff"' in cropped,
          "footprint picture: page fitted to the drawing, on white", cropped)
    back = ('(footprint "L:F"\n\t(layer "B.Cu")\n\t(at 10 20 90)\n'
            '\t(pad "1" smd rect (at 1 -2 120) (layers "B.Cu" "B.Mask"))\n'
            '\t(fp_line (start 0 -1) (end 2 -3) (layer "B.SilkS"))\n'
            '\t(fp_text user "x" (at 0 0) (layer "B.Fab") (effects (justify mirror)))\n)')
    top = WZ.FP._as_library(back)
    check('(pad "1" smd rect (at 1 2 330) (layers "F.Cu" "F.Mask"))' in top
          and "(start 0 1) (end 2 3)" in top and '"F.SilkS"' in top
          and "mirror" not in top and '(layer "F.Cu")' in top,
          "footprint picture: a bottom part is drawn from its own top", top)


def _tiny_pdf(text):
    """A one-page PDF that pdftotext reads as `text`."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1, xref)
    return out


def test_web_front_end_asks_the_same_questions():
    """wizard --gui: the same Wizard behind webui. Answers come in over HTTP
    and land in the same files; a wrong token gets nothing."""
    import io
    import json
    import re
    import tempfile
    import threading
    import urllib.error
    import urllib.request
    import todo as TD
    import webui
    import wizard as WZ
    comps, _ = N.parse(os.path.join(FIX, "tmux_broken.xml"))
    att = os.path.join(tempfile.mkdtemp(), "attestations")
    draft = os.path.join(att, "reference", "draft")
    os.makedirs(draft)
    stub = os.path.join(draft, "STUB-TMUX1309APWR.toml")
    with open(stub, "w") as f:
        f.write('doc_id = ""\norigin = "manual"\nextracted_by = ""\n\n'
                '  [[variant]]\n  package = "TSSOP-16"\n'
                '  mpns = ["TMUX1309APWR"]\n  pin_count = 16\n\n'
                '    [variant.pins]\n'
                + "".join(f'    {n} = ""\n' for n in range(1, 17)))
    WZ._find_pdf = lambda *a: ""
    lines, items = TD.collect(comps, att)
    ui = webui.WebUI(echo=lambda t: None)
    cache = tempfile.mkdtemp()
    w = WZ.Wizard(att, att, lines, items, auto_open=False, by="Tester", ui=ui,
                  cache=cache)
    out = io.StringIO()
    t = threading.Thread(target=webui.serve, args=(w, ui),
                         kwargs={"open_browser": False, "out": out, "port": 0,
                                 "token": "test-token"}, daemon=True)
    t.start()
    for _ in range(100):
        if "http" in out.getvalue():
            break
        threading.Event().wait(0.05)
    url = re.search(r"http://\S+", out.getvalue()).group(0)

    def state(v=-1):
        return json.load(urllib.request.urlopen(f"{url}state?v={v}", timeout=10))

    def answer(value):
        s = state()
        while not s["question"] and not s["done"]:
            s = state(s["version"])
        req = urllib.request.Request(f"{url}answer", json.dumps(
            {"id": s["question"]["id"], "value": value}).encode())
        return s["question"], json.load(urllib.request.urlopen(req, timeout=10))

    bad = url.rsplit("/", 2)[0] + "/wrong/state"
    try:
        urllib.request.urlopen(bad, timeout=5)
        check(False, "web: a wrong token is refused")
    except urllib.error.HTTPError as e:
        check(e.code == 404, "web: a wrong token is refused", str(e))
    s = state()
    while not s["question"]:
        s = state(s["version"])
    ti = "https://www.ti.com/lit/ds/symlink/tmux1309a.pdf"
    check(s["pdf"] and s["pdf"].get("remote") == ti,
          "web: not downloaded, so the page shows the Datasheet URL", str(s["pdf"]))

    def drop(body, ctype):
        req = urllib.request.Request(f"{url}drop", body,
                                     {"Content-Type": ctype, "X-Name": "tmux.pdf"})
        return json.load(urllib.request.urlopen(req, timeout=10))

    check(not drop(b"not a url", "text/plain")["ok"]
          and not drop(b"<html>", "application/pdf")["ok"],
          "web: a drop that is neither a URL nor a PDF is refused")
    junk = os.path.join(cache, "not a pdf.txt")
    with open(junk, "w") as f:
        f.write("hello")
    check(not drop(b"file:///nonexistent/x.pdf", "text/plain")["ok"]
          and not drop(b"file://" + junk.replace(" ", "%20").encode(),
                       "text/plain")["ok"],
          "web: a file:// URL that is missing or not a PDF is refused")
    pdf = os.path.join(cache, "my download.pdf")
    with open(pdf, "wb") as f:
        f.write(_tiny_pdf("TMUX1309APWR 8:1 multiplexer"))
    check(drop(b"file://" + pdf.replace(" ", "%20").encode() + b"#page=7",
               "text/plain")["ok"],
          "web: a pasted file:// URL of a downloaded PDF is taken")
    s = state()
    while not (s["question"] and s["pdf"] and s["pdf"].get("id")):
        s = state(s["version"])
    stem = D._cache_stem(ti, cache)
    check(os.path.isfile(stem + ".pdf") and s["question"]["prompt"] == "pin 1",
          "web: the dropped PDF is filed under the Datasheet URL, shown, and "
          "the question asked again", str(s["pdf"]))
    q, r = answer("S0B")
    check(q["prompt"] == "pin 1" and q["text"] and r["ok"],
          "web: the first question is pin 1, typed", str(q))
    acts = [v for v, _l in q["actions"]]
    check("-" in acts and ":n" in acts and ":s" in acts,
          "web: a pin prompt offers not connected, numbers only and skip "
          "as buttons, like the commands a terminal user types", str(acts))
    q, r = answer("q")                  # a pin may be called q
    q, r = answer(":q")
    t.join(10)
    text = open(stub).read()
    check('1 = "S0B"' in text and '2 = "q"' in text,
          "web: typed names are saved like the terminal saves them", text[-300:])
    check(not t.is_alive(), "web: the server ends with the wizard")


# A 2x5 header whose symbol follows a (made-up) interface spec SLX: odd
# pins are signals, every even pin is GND. Pin 5 is IO_4 on the symbol,
# IO_3 in the spec: the kind of swap an interface table exists to catch.
_SLX_NAMES = {"1": "VBAT", "3": "IO_1", "5": "IO_4", "7": "IO_2", "9": "5V5",
              "2": "GND", "4": "GND", "6": "GND", "8": "GND", "10": "GND"}
_SLX_SPEC = """# SLX interface

## FFC

| Pin | Signal |
|---|---|
| 1 | `GND` |
| 2 | `VBAT` |

## 2x5 header

Every even pin is `GND`. The odd pins:

| Pin | Signal | Use |
|---|---|---|
| 1 | `VBAT` | |
| 3 | `IO_1` | UART |
| 5 | `IO_3` | |
| 7 | `IO_2` | |
| 9 | `5V5` | |
"""


def _slx_netlist(path, interface="SLX", names=None):
    names = names or _SLX_NAMES
    field = (f'<field name="Pinout_Spec">{interface}</field>' if interface else "")
    pins = "".join(f'<pin num="{n}" name="{v}" type="passive"/>'
                   for n, v in names.items())
    open(path, "w").write(
        '<?xml version="1.0"?><export version="E"><components>'
        '<comp ref="J1"><value>SLX</value>'
        '<footprint>Connector_PinSocket_2.54mm:PinSocket_2x05_P2.54mm_Vertical</footprint>'
        '<fields><field name="MPN">SSW-105-01-T-D</field>'
        '<field name="Datasheet">https://example.com/ssw.pdf</field></fields>'
        '<libsource lib="Jitter_Connectors" part="SLX_Header_2x5"/></comp>'
        '</components><libparts>'
        '<libpart lib="Jitter_Connectors" part="SLX_Header_2x5">'
        f'<fields>{field}</fields><pins>{pins}</pins></libpart>'
        '</libparts></export>')
    comps, _ = N.parse(path)
    return comps


def test_interface_tables():
    """A connector whose datasheet only numbers its contacts, on a symbol
    that names them after an interface. The part table checks the
    numbering; a signed interface table, keyed to the symbol, checks the
    names. Nothing aliases the symbol's names wholesale."""
    import tempfile
    import interface as IF
    import todo as TD
    import wizard as WZ
    import cli
    top = tempfile.mkdtemp()
    att = os.path.join(top, "attestations")
    draft = os.path.join(att, "reference", "draft")
    os.makedirs(draft)
    xml = os.path.join(top, "n.xml")
    comps = _slx_netlist(xml)
    check(TD.interface_of(comps[0]) == "SLX",
          "interface: the field is read from the library symbol")

    # -- the spec reader: hints only, from the right table ------------------
    tables = IF.spec_tables(_SLX_SPEC)
    check([h for h, _t in tables] == ["FFC", "2x5 header"]
          and tables[1][1]["5"] == "IO_3",
          "interface: pin tables are read from a markdown spec", str(tables))
    check(IF.parse_shared("GND = 2-10/2", set("2468") | {"10"})[1:]
          == (["2", "4", "6", "8", "10"], []),
          "interface: one name for many pins, with a step")
    check(IF.parse_shared("GND = 2 12", {"2"})[2] == ["12"],
          "interface: a pin the part does not have is refused")

    # -- part table: "only numbers" on a symbol that names its pins ---------
    stub = os.path.join(draft, "STUB-SSW-105-01-T-D.toml")
    open(stub, "w").write(
        'doc_id = ""\norigin = "manual"\nextracted_by = ""\n\n'
        '  [[variant]]\n  mpns = ["SSW-105-01-T-D"]\n  pin_count = 10\n\n'
        '    [variant.pins]\n' + "".join(f'    {n} = ""\n' for n in _SLX_NAMES))
    lines, items = TD.collect(comps, att)
    check([i.kind for i in items] == ["stub_unfilled", "interface_unfilled"],
          "interface: a stub and the missing interface table are open",
          str(items))
    WZ._find_pdf = lambda *a: ""
    answers = iter(["u", "", "y"])   # only numbers; nothing joined; same pin 1
    said = []
    w = WZ.Wizard(att, top, lines, items, ask=lambda p: next(answers),
                  say=said.append, auto_open=False, by="T")
    w.do_stub_unfilled(items[0], lines[0])
    path = os.path.join(att, "reference", "datasheet", "SSW-105-01-T-D.toml")
    var = P.Document(path, tomlite.load(path)).variants[0]
    check(var.numbered and not var.alias and var.pad_layout == "same"
          and var.joined == [],
          "interface: the part table claims the numbering, no aliases",
          open(path).read())
    check("does not check the symbol's pin names" in " ".join(
              x.strip() for x in said),
          "interface: the wizard says the names are not checked by it")

    # -- without the field, the names stay open and status says what to do -
    bare = _slx_netlist(os.path.join(top, "bare.xml"), interface="")
    _l, its = TD.collect(bare, att)
    check([i.kind for i in its] == ["interface_unset"]
          and "Pinout_Spec = <name>" in its[0].detail,
          "interface: without the field the names are open, with what to do",
          str([(i.kind, i.detail) for i in its]))
    numbered_sym = _slx_netlist(os.path.join(top, "num.xml"), interface="",
                                names={n: n for n in _SLX_NAMES})
    check(TD.collect(numbered_sym, att)[1] == [],
          "interface: a symbol that names no pins needs no interface")

    # -- the interface table: spec, hints confirmed one by one, sign ---------
    spec = os.path.join(top, "slx.md")
    open(spec, "w").write(_SLX_SPEC)
    lines, items = TD.collect(comps, att)
    check([i.kind for i in items] == ["interface_unfilled"],
          "interface: only the interface table is open now", str(items))
    seen = []
    answers = iter([spec, "2",                 # the spec, its header table
                    "", "GND", "", "GND", "",  # pins 1-5: Enter takes the hint
                    "GND", "", "GND", "", "GND",
                    "y"])                      # pin 5 differs: fix the symbol

    def ask(prompt):
        seen.append(prompt)
        return next(answers)
    w = WZ.Wizard(att, top, lines, items, ask=ask, say=said.append,
                  auto_open=False, by="T")
    w.do_interface_unfilled(items[0], lines[0])
    check(any("(IO_3)" in x for x in seen) and not any("IO_4" in x for x in seen),
          "interface: the spec's name is offered, never the symbol's", str(seen))
    signed = os.path.join(att, "reference", "interface", "SLX-SLX_Header_2x5.toml")
    doc = P.Document(signed, tomlite.load(signed))
    check(doc.reviewed_by == "T" and doc.interface == "SLX"
          and doc.spec == os.path.relpath(spec, os.path.dirname(signed))
          and doc.variants[0].pins["5"] == "IO_3"
          and doc.variants[0].symbols == ["Jitter_Connectors:SLX_Header_2x5"],
          "interface: signed, keyed to the symbol, spec kept", open(signed).read())

    # -- the swap is found and reported like a datasheet difference ----------
    lines, items = TD.collect(comps, att)
    check([(i.kind, i.code) for i in items]
          == [("interface_difference", "interface_difference")]
          and items[0].findings[0]["pin"] == "5",
          "interface: the symbol's swapped name is a difference", str(items))
    L = []
    e, _w = cli.interface_report(L, comps[0], lines[0].table,
                                 TD.interface_docs(att))
    check(e == 1 and any("interface_mismatch" in x for x in L),
          "report: the interface difference is an error", "\n".join(L))

    # -- a fixed symbol passes both tables -----------------------------------
    fixed = _slx_netlist(os.path.join(top, "fixed.xml"),
                         names=dict(_SLX_NAMES, **{"5": "IO_3"}))
    lines, items = TD.collect(fixed, att)
    check(items == [] and len(TD.done_lines(lines, items)) == 1,
          "interface: a symbol that agrees with both tables is done", str(items))
    L = []
    e, _w = cli.interface_report(L, fixed[0], lines[0].table,
                                 TD.interface_docs(att))
    check(e == 0 and any("**OK**" in x for x in L), "report: interface OK",
          "\n".join(L))
    L = []
    e, _w = cli.interface_report(L, bare[0], lines[0].table, [])
    check(e == 1 and any("interface_unset" in x for x in L),
          "report: names nothing checks are an error")

    # -- the browser shows the spec, markdown as text ------------------------
    import webui
    ui = webui.WebUI(echo=lambda t: None)
    ui.show_doc(spec, "SLX spec")
    check(ui.pdf["doc"] and ui.pdf["url"].endswith("/slx.md"),
          "webui: a markdown spec is shown in the document pane", str(ui.pdf))
    ui.show_doc("https://example.com/slx.md")
    check(ui.pdf["remote"] == "https://example.com/slx.md" and ui.pdf["doc"],
          "webui: a spec URL opens in the pane")

    # -- a draft only holds hints until a person answers ---------------------
    d = os.path.join(top, "d.toml")
    IF.write_stub(d, "SLX", "L:S", ["1", "3"], spec, {"1": "VBAT"}, "slx.md")
    v = P.Document(d, tomlite.load(d)).variants[0]
    check(v.pins == {"1": "", "3": ""} and IF.hints_of(d) == {"1": "VBAT"},
          "interface: a machine-read name is a hint, never a value")


def _alias_netlist(path, part, names):
    """One part, MPN M, on symbol L:<part> with these pin names."""
    pins = "".join(f'<pin num="{n}" name="{v}" type="passive"/>'
                   for n, v in names.items())
    open(path, "w").write(
        '<?xml version="1.0"?><export version="E"><components>'
        '<comp ref="U1"><value>M</value><footprint>P:SOT-23</footprint>'
        '<fields><field name="MPN">M</field>'
        '<field name="Datasheet">https://example.com/m.pdf</field></fields>'
        f'<libsource lib="L" part="{part}"/></comp></components><libparts>'
        f'<libpart lib="L" part="{part}"><pins>{pins}</pins></libpart>'
        '</libparts></export>')
    return N.parse(path)[0]


def test_alias_blocks_name_their_symbols():
    """An alias names a symbol's pin, so it holds for the symbol it was
    accepted on. A block that lists its symbols does not apply to another
    symbol of the same part: that is its own item, and the wizard asks
    whether the other symbol has the same pinout."""
    import tempfile
    import threading
    import todo as TD
    import webui
    import wizard as WZ
    import reffile as R
    top = tempfile.mkdtemp()
    att = os.path.join(top, "attestations")
    live = os.path.join(att, "reference", "datasheet")
    os.makedirs(live)
    ref = os.path.join(live, "M.toml")
    head = ('doc_id = "X"\norigin = "manual"\nextracted_by = "T"\n'
            'reviewed_by = "T"\nreviewed_on = "2026-11-25"\n\n'
            '  [[variant]]\n  package = "SOT-23"\n  mpns = ["M"]\n\n'
            '    [variant.pins]\n    1 = "VFB"\n    2 = "COMP"\n    3 = "AGND"\n')
    a_names = {"1": "FB", "2": "CMP", "3": "AGND"}
    sym_a = _alias_netlist(os.path.join(top, "a.xml"), "A", a_names)
    sym_b = _alias_netlist(os.path.join(top, "b.xml"), "B", a_names)
    kinds = lambda comps: [i.kind for i in TD.collect(comps, att)[1]]

    # -- an old file: [variant.symbol_alias] holds for any symbol -------------
    open(ref, "w").write(head + '\n    [variant.symbol_alias]\n'
                         '    1 = "FB"\n    2 = "CMP"\n')
    var = P.Document(ref, tomlite.load(ref)).variants[0]
    check(kinds(sym_a) == [] and kinds(sym_b) == []
          and P.compare(var, sym_b[0].pins, "L:B") == P.compare(var, sym_b[0].pins),
          "alias blocks: a file without symbols checks as before",
          str(kinds(sym_b)))

    # -- a block for A: passes on A, its own item on B ------------------------
    open(ref, "w").write(head + '\n    [[variant.alias_for]]\n'
                         '    symbols = ["L:A"]\n\n    [variant.alias_for.pins]\n'
                         '    1 = "FB"\n    2 = "CMP"\n')
    check(kinds(sym_a) == [], "alias blocks: a block for A passes on A",
          str(kinds(sym_a)))
    _l, its = TD.collect(sym_b, att)
    check([i.kind for i in its] == ["alias_other_symbol"]
          and "L:A" in its[0].detail and "L:B" in its[0].detail
          and {f["pin"] for f in its[0].findings} == {"1", "2"},
          "alias blocks: a block for A on B is its own item, naming both",
          str([(i.kind, i.detail) for i in its]))
    bare = os.path.join(top, "bare.toml")
    open(bare, "w").write(head + '\n    [[variant.alias_for]]\n'
                          '    symbols = ["A"]\n\n    [variant.alias_for.pins]\n'
                          '    1 = "FB"\n    2 = "CMP"\n')
    bv = P.Document(bare, tomlite.load(bare)).variants[0]
    check(not P.compare(bv, sym_a[0].pins, "L:A")
          and P.compare(bv, sym_a[0].pins, "Other:B"),
          "alias blocks: a bare symbol name matches it in any library")

    # -- wizard, "same": B is added to A's block ------------------------------
    def wizard(comps, answers, ui=None):
        lines, items = TD.collect(comps, att)
        feed, said, asked = iter(answers), [], []
        w = WZ.Wizard(att, top, lines, items, auto_open=False, by="T", ui=ui,
                      ask=lambda p: (asked.append(p), next(feed))[1],
                      say=said.append)
        w.do_alias_other_symbol(items[0], lines[0])
        return said, asked
    WZ._find_pdf = lambda *a: ""
    said, asked = wizard(sym_b, [""])
    var = P.Document(ref, tomlite.load(ref)).variants[0]
    check(any("name these pins the same" in x for x in said)
          and any("(y)" in x for x in asked)
          and [b.symbols for b in var.alias_for] == [["L:A", "L:B"]]
          and kinds(sym_b) == [],
          "alias blocks: wizard, same pinout: B added to A's block, the "
          "default", "\n".join(said) + open(ref).read())

    # -- wizard, differing: a second block for B ------------------------------
    open(ref, "w").write(head + '\n    [[variant.alias_for]]\n'
                         '    symbols = ["L:A"]\n\n    [variant.alias_for.pins]\n'
                         '    1 = "FB"\n    2 = "CMP"\n')
    sym_c = _alias_netlist(os.path.join(top, "c.xml"), "C",
                           {"1": "FBX", "2": "CMP", "3": "AGND"})
    said, asked = wizard(sym_c, ["", "FBX", ""])   # Enter = "differs", A's CMP
    var = P.Document(ref, tomlite.load(ref)).variants[0]
    check(any("(n)" in x for x in asked) and any("(FB)" in x for x in asked)
          and [(b.symbols, b.pins) for b in var.alias_for]
          == [(["L:A"], {"1": "FB", "2": "CMP"}),
              (["L:C"], {"1": "FBX", "2": "CMP"})]
          and kinds(sym_c) == [] and kinds(sym_a) == [],
          "alias blocks: wizard, differing pinout: a second block for C, "
          "A's names offered", "\n".join(asked) + open(ref).read())

    # -- the wizard writes the symbol into an alias it accepts ----------------
    sym_d = _alias_netlist(os.path.join(top, "d.xml"), "D",
                           {"1": "FB", "2": "COMP", "3": "AGND"})
    R.add_alias_block(ref, "M", "L:D")   # D looked at, nothing accepted yet
    lines, items = TD.collect(sym_d, att)
    check([i.kind for i in items] == ["pin_difference"],
          "alias blocks: with its own block, the rest is a plain difference",
          str(items))
    w = WZ.Wizard(att, top, lines, items, auto_open=False, by="T",
                  ask=lambda p: "a", say=lambda t: None)
    w.do_pin_difference(items[0], lines[0])
    var = P.Document(ref, tomlite.load(ref)).variants[0]
    check(var.alias_for[-1].symbols == ["L:D"]
          and var.alias_for[-1].pins == {"1": "FB"} and not var.alias,
          "alias blocks: an alias the wizard accepts names the symbol",
          open(ref).read())

    # -- the web page: the same question, the choices as buttons --------------
    open(ref, "w").write(head + '\n    [[variant.alias_for]]\n'
                         '    symbols = ["L:A"]\n\n    [variant.alias_for.pins]\n'
                         '    1 = "FB"\n    2 = "CMP"\n')
    ui = webui.WebUI(echo=lambda t: None)
    t = threading.Thread(target=wizard, args=(sym_b, [], ui), daemon=True)
    t.start()
    s = ui.state()
    while not s["question"]:
        s = ui.state(s["version"], timeout=5)
    q = s["question"]
    ui.submit(q["id"], "y")
    t.join(10)
    check([b[0] for b in q["buttons"]] == ["y", "n"] and q["default"] == "y"
          and "same: add L:B to the block" in dict(q["buttons"]).values()
          and not q["text"] and not t.is_alive()
          and P.Document(ref, tomlite.load(ref)).variants[0].alias_for[0].symbols
          == ["L:A", "L:B"],
          "alias blocks: web page asks it with buttons, and the answer lands",
          str(q))
    check(any(v == "-" for v, _l in WZ.Wizard(att, top, [], []).buttons_for(
              [], {}, [("-", "no alias")])[1]),
          "alias blocks: no alias is a button where a name is typed")


def test_two_clones_of_one_library():
    """--library names the clone tables are signed into; the board also pins
    the same library as a submodule. Each table is read once, from the
    --library clone."""
    import shutil
    import tempfile
    import library as LIB
    import todo as TD
    base = tempfile.mkdtemp()
    roots = []
    for name, by in (("work", "new"), ("pinned", "old")):
        d = os.path.join(base, name, "attestations", "reference", "datasheet")
        os.makedirs(d)
        shutil.copy(os.path.join(FIX, "reference", "TMUX1309APWR.toml"), d)
        f = os.path.join(d, "TMUX1309APWR.toml")
        text = open(f).read()
        open(f, "w").write(f"# {by}\n" + text)
        roots.append(os.path.join(base, name))
    att = os.path.join(base, "board", "attestations")
    os.makedirs(att)
    saved = LIB.read_roots
    LIB.read_roots = lambda project: roots
    try:
        docs = TD.signed_docs(att, "board")
    finally:
        LIB.read_roots = saved
    check(len(docs) == 1 and "/work/" in docs[0][0],
          "library: a table in two clones is read once, from the first root",
          str([f for f, _d in docs]))


def test_signed_tables_stay_valid():
    """A table signed for a released board must keep passing in every later
    bomcheck. tests/fixtures/compat/<date>/ holds the signed tables of that
    date and boards that use them: each board stays fully checked. Never edit
    those files; a format change adds a new folder instead."""
    import glob
    import shutil
    import tempfile
    import todo as TD
    for snap in sorted(glob.glob(os.path.join(FIX, "compat", "*"))):
        att = os.path.join(snap, "attestations")
        for f in glob.glob(os.path.join(att, "reference", "*", "*.toml")):
            P.Document(f, tomlite.load(f))     # every table still loads
        for board in sorted(glob.glob(os.path.join(snap, "*", "netlist.xml"))):
            where = os.path.dirname(board)
            comps, _ = N.parse(board)
            _lines, items = TD.collect(comps, att, where)
            check(comps and not items,
                  f"compat {os.path.basename(snap)}/{os.path.basename(where)}: "
                  f"every part still checked against its signed table",
                  str([(i.kind, i.mpn, i.detail) for i in items][:5]))
    # The check is not empty: a renamed pin on the symbol is still caught.
    snap = sorted(glob.glob(os.path.join(FIX, "compat", "*")))[0]
    tmp = tempfile.mkdtemp()
    text = open(os.path.join(snap, "sl23-sen6x", "netlist.xml")).read()
    open(os.path.join(tmp, "netlist.xml"), "w").write(
        text.replace('name="IO_13"', 'name="IO_31"', 1))
    comps, _ = N.parse(os.path.join(tmp, "netlist.xml"))
    _lines, items = TD.collect(comps, os.path.join(snap, "attestations"), tmp)
    check(any(i.kind.startswith("interface") for i in items),
          "compat: a pin renamed on the symbol is still reported",
          str([(i.kind, i.mpn) for i in items]))


def main():
    for fn in (test_signed_tables_stay_valid, test_two_clones_of_one_library, test_defect_is_caught, test_polarity_is_not_lost,
               test_provenance_rejects_known_bad_pdfs, test_whole_token_matching,
               test_near_family_never_merges, test_enumeration_xor_decoder,
               test_extract_matches_hand_transcription,
               test_extract_number_first_full_word_types,
               test_multipackage_column_choice, test_wrapped_name_recovery,
               test_order_code_decoding, test_url_patterns_are_offered_not_trusted,
               test_schematic_property_write,
               test_symbol_serving_several_designators,
               test_differential_naming_convention,
               test_spi_role_is_read_not_assumed,
               test_conventions_live_in_matching_not_extraction,
               test_parameter_rows_are_not_pins,
               test_alias_is_an_equivalence_not_a_waiver,
               test_wizard_cannot_bless_a_wrong_symbol,
               test_login_gated_datasheet_is_filed_under_its_url,
               test_datasheet_rule_and_exemptions,
               test_active_low_spellings,
               test_wizard_opens_the_pinout_page,
               test_partial_fill_is_not_a_conflict,
               test_decisions_survive_regeneration,
               test_decisions_round_trip, test_bom_field_suggestions, test_family_stem_must_be_distinctive, test_dnp_parts_are_out_of_scope,
               test_subject_check_catches_a_mislinked_datasheet,
               test_mpn_falls_back_to_value_but_says_so,
               test_stage_one_never_sees_the_symbol, test_one_document_two_parts,
               test_shape_name_then_columns, test_shape_number_name_only,
               test_name_then_columns_uses_ordinal_position,
               test_non_numeric_pin_ids_rejected,
               test_column_shape_needs_two_real_columns,
               test_gate_needs_a_nearly_complete_table, test_driver_gates_scavengers,
               test_driver_refuses_overextraction,
               test_package_scoped_tables, test_three_column_table_is_not_skipped,
               test_last_section_does_not_run_to_eof,
               test_overextraction_is_refused,
               test_extract_name_plus_numlist, test_range_expansion_is_safe,
               test_prose_ranges_are_recovered, test_series_document_vs_wrong_family,
               test_incomplete_reference_is_detected, test_pinmapped_discriminator,
               test_register_table_is_not_a_pin_table,
               test_pinout_drawing_needs_the_ladder,
               test_shared_datasheet_scopes_by_device,
               test_header_table_reads_by_column,
               test_exposed_pad_is_numbered_from_the_footprint,
               test_quectel_sections_are_one_table,
               test_st_names_centred_on_their_row, test_pin_count_from_the_document,
               test_decimal_comma_is_not_a_pin_list,
               test_one_impossible_pin_discards_the_reading,
               test_ground_and_spi_synonyms,
               test_wizard_exits_cleanly_and_pin_names_are_free,
               test_wizard_back_undoes_the_part,
        test_wizard_asks_again_what_is_still_open, test_design_sheets_only,
        test_tables_in_the_component_library, test_review_folder_stays_out_of_git, test_apply_ignores_other_projects_designators,
        test_sim_holder_contact_numbers,
               test_symbol_drawing_is_read, test_dual_part_units_must_be_one_device,
               test_choke_connections_against_symbol,
               test_wizard_asks_a_chokes_windings,
               test_marking_question_and_footprint_mark,
               test_polarity_parts, test_waivers,
               test_half_typed_pin_stub_on_polar_part,
               test_wizard_closes_its_viewers,
               test_web_front_end_asks_the_same_questions,
               test_interface_tables,
               test_alias_blocks_name_their_symbols):
        if "-v" in sys.argv:
            print(f"{fn.__name__}:")
        try:
            fn()
        except Exception as e:                                   # noqa: BLE001
            FAILS.append(f"{fn.__name__} raised {type(e).__name__}: {e}")
    if FAILS:
        for f in FAILS:
            print(f"FAIL {f}")
        print(f"\nspike: {len(FAILS)} FAILED")
        return 1
    print("spike: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
