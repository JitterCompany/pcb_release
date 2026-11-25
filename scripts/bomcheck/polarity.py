#!/usr/bin/env python3
"""Two-pin parts where only the polarity can be checked.

A diode's datasheet numbers no pins; it says what the band on the part means.
So for these parts the claim is not a pin table but three links:

    symbol    the cathode (or +) pin has number N
    footprint pad N is the pad its silk/fab marking points at
    datasheet the band on the part marks the cathode (or anode, +, -)

The class comes from the symbol's own data: pins named K and A, a
bidirectional symbol, or a symbol that calls itself a polarized capacitor.
"""
import re

MARKS = ("cathode", "anode", "plus", "minus", "none")

POLAR_DIODE, POLAR_CAP, BIDIRECTIONAL = "polar diode", "polarized cap", "bidirectional"


def classify(comp):
    """-> POLAR_DIODE, POLAR_CAP, BIDIRECTIONAL or "" for a placed part."""
    pins = comp.pins
    if len(pins) != 2:
        return ""
    names = {p.name.strip().upper() for p in pins}
    desc = (comp.libpart.fields.get("Description", "") if comp.libpart else "").lower()
    if names in ({"K", "A"}, {"CATHODE", "ANODE"}) or "unidirectional" in desc:
        return POLAR_DIODE
    if names == {"A1", "A2"} or "bidirectional" in desc:
        return BIDIRECTIONAL
    if re.search(r"\bpolari[sz]ed\b", desc) and "unpolari" not in desc:
        return POLAR_CAP
    return ""


def marked_pin(comp, klass, marked="", plus_sign=False):
    """The symbol pin the footprint's mark belongs at.

    A + sign on the footprint is the + pin, whatever the part shows. A band
    or bar on the footprint is where the part's own mark goes, so it is the
    pin the datasheet's mark is on (`marked`); without that answer, KiCad's
    convention: the cathode, or the + of a capacitor."""
    if klass == POLAR_CAP:
        plus = next((p.num for p in comp.pins if p.name.strip() == "+"), "1")
        if marked == "minus" and not plus_sign:
            return next((p.num for p in comp.pins if p.num != plus), "")
        return plus
    if klass == POLAR_DIODE:
        # "" when a polar symbol does not name its cathode (KiCad's SMAJ13A
        # says unidirectional and names its pins A1 and A2).
        names = ("A", "ANODE") if marked == "anode" else ("K", "CATHODE")
        return next((p.num for p in comp.pins
                     if p.name.strip().upper() in names), "")
    return ""


def expected_mark(klass, marked="", plus_sign=False):
    """Which pin the footprint's mark stands for, in words."""
    if klass == POLAR_CAP:
        return "− pin" if marked == "minus" and not plus_sign else "+ pin"
    if klass == POLAR_DIODE:
        return "anode" if marked == "anode" else "cathode"
    return ""


_WORD = {"cathode": "cathode", "anode": "anode", "positive": "plus",
         "negative": "minus", "+": "plus", "-": "minus"}
_PATTERNS = [
    re.compile(r"\b(?:colou?r\s+)?(?:band|stripe|bar|dot|notch|marking)\s+"
               r"(?:denotes|indicates|identifies|shows|marks|is|=)\s+(?:the\s+)?"
               r"(cathode|anode|positive|negative)\b", re.I),
    re.compile(r"\b(cathode|anode|positive|negative)\s+"
               r"(?:band|stripe|mark(?:ing)?|indicator|side)\b", re.I),
]


def datasheet_hint(text):
    """-> (mark, evidence, page) read from the datasheet text, or ("", "", 0).

    A suggestion for a person to confirm, never a verdict: "band denotes
    cathode except bipolar" is true for one part of a family and not another.
    """
    found = []
    for n, page in enumerate(text.split("\f"), 1):
        for rx in _PATTERNS:
            for m in rx.finditer(page):
                line = page[max(0, page.rfind("\n", 0, m.start()) + 1):
                            page.find("\n", m.end()) % (len(page) + 1)]
                clean = re.sub(r"[^\x20-\x7e]+", " ", line)
                found.append((_WORD[m.group(1).lower()],
                              re.sub(r"\s+", " ", clean).strip()[:100], n))
    marks = {f[0] for f in found}
    if len(marks) != 1:
        return "", "; ".join(f"{e} (p{p})" for _m, e, p in found[:2]), 0
    mark, evidence, page = found[0]
    return mark, evidence, page
