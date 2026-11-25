#!/usr/bin/env python3
"""Propose the BOM fields a symbol is missing, so KiCad can be the source of truth.

THE GOAL. Manufacturer, MPN and datasheet URL should live in the schematic. That
is what makes the factory order the part we designed against, and it is what
lets any later check -- pinout, package, obsolescence -- start from the design
rather than from somebody's memory. Symbols often lack a datasheet URL, and
some lack an MPN field.

WHAT THIS DOES NOT DO. It does not edit the schematic. Every output is a
SUGGESTION with the evidence that produced it, for a human to accept or correct;
a tool that silently wrote a guessed MPN into a design would be worse than the
gap it filled.

EVIDENCE, STRONGEST FIRST. A suggestion is only as good as its reason, so the
reason travels with it:

  * the datasheet that VERIFIED for this part gives the URL directly, and its
    host gives the manufacturer -- ti.com is Texas Instruments and nothing else;
  * the Value field gives an MPN when it plausibly holds one (`BC857BS`),
    and never when it holds a functional name (`swd_with_uart_client`);
  * the manufacturer name found in the datasheet text corroborates the host.

Nothing is inferred from the MPN's shape alone. Prefix tables ("anything
starting with TPS is TI") are exactly the kind of lore that rots quietly, and a
wrong manufacturer is harder to notice than a missing one.
"""
import re

# Datasheet host -> manufacturer. A host is owned by one company, which makes
# this a fact about the URL rather than a guess about the part number.
HOST_VENDOR = {
    "ti.com": "Texas Instruments",
    "st.com": "STMicroelectronics",
    "diodes.com": "Diodes Incorporated",
    "nexperia.com": "Nexperia",
    "we-online.com": "Würth Elektronik",
    "we-online.de": "Würth Elektronik",
    "quectel.com": "Quectel",
    "vishay.com": "Vishay",
    "renesas.com": "Renesas",
    "microchip.com": "Microchip Technology",
    "infineon.com": "Infineon Technologies",
    "cypress.com": "Infineon Technologies",
    "littelfuse.com": "Littelfuse",
    "onsemi.com": "onsemi",
    "rohm.com": "ROHM Semiconductor",
    "murata.com": "Murata",
    "tdk.com": "TDK",
    "samsung-fp.com": "Samsung Electro-Mechanics",
    "hirose.com": "Hirose Electric",
    "molex.com": "Molex",
    "amphenol.com": "Amphenol",
}

# Names as they appear IN a datasheet, used only to corroborate the host.
VENDOR_IN_TEXT = {
    "Texas Instruments": ("TEXAS INSTRUMENTS",),
    "STMicroelectronics": ("STMICROELECTRONICS",),
    "Diodes Incorporated": ("DIODES INCORPORATED", "DIODES INC"),
    "Nexperia": ("NEXPERIA",),
    "Würth Elektronik": ("WÜRTH ELEKTRONIK", "WURTH ELEKTRONIK", "WE ONLINE"),
    "Quectel": ("QUECTEL",),
    "Vishay": ("VISHAY",),
    "Renesas": ("RENESAS", "DIALOG SEMICONDUCTOR"),
    "Microchip Technology": ("MICROCHIP",),
    "Infineon Technologies": ("INFINEON", "CYPRESS"),
    "Littelfuse": ("LITTELFUSE",),
}


def vendor_from_url(url):
    """-> (manufacturer, why) or (None, "")."""
    m = re.match(r"https?://([^/]+)/", str(url) or "")
    if not m:
        return None, ""
    # NOT lstrip("www."): that strips CHARACTERS, so "www.we-online.com"
    # becomes "e-online.com" and never matches its own vendor.
    host = re.sub(r"^www\.", "", m.group(1).lower())
    for suffix, vendor in HOST_VENDOR.items():
        if host == suffix or host.endswith("." + suffix) or suffix in host:
            return vendor, f"the datasheet is hosted on {host}"
    return None, ""


def vendor_from_text(text, limit=200_000):
    """-> (manufacturer, why) or (None, ""). Corroboration, not primary."""
    up = (text or "")[:limit].upper()
    for vendor, needles in VENDOR_IN_TEXT.items():
        for n in needles:
            if n in up:
                return vendor, f"the datasheet text names {vendor}"
    return None, ""


class Suggestion:
    __slots__ = ("field", "value", "why", "confidence")

    def __init__(self, field, value, why, confidence):
        self.field, self.value = field, value
        self.why, self.confidence = why, confidence

    def __repr__(self):
        return f"Suggestion({self.field}={self.value!r} [{self.confidence}])"


def for_component(comp, doc_url="", doc_text="", doc_verdict=""):
    """-> [Suggestion] for the fields this symbol is missing.

    `doc_*` describe a datasheet that already VERIFIED for this part; passing an
    unverified document would turn a suggestion into a rumour, so the caller is
    expected to have run the provenance check first.
    """
    out = []
    mpn, source = comp.mpn_effective

    if not comp.mpn:
        if mpn:
            out.append(Suggestion(
                "MPN", mpn,
                f"the Value field holds {mpn!r}, which is shaped like a part "
                f"number; the MPN field is empty",
                "medium"))
        else:
            out.append(Suggestion(
                "MPN", "", "no MPN field and the Value field is not a part "
                "number -- this one needs a human", "none"))

    if not comp.field("Manufacturer"):
        vendor, why = vendor_from_url(doc_url)
        conf = "high"
        if not vendor:
            vendor, why = vendor_from_text(doc_text)
            conf = "medium"
        if vendor:
            out.append(Suggestion("Manufacturer", vendor, why, conf))
        else:
            out.append(Suggestion("Manufacturer", "",
                                  "no datasheet host or vendor name to go on",
                                  "none"))

    if not str(comp.field("Datasheet")).startswith("http") and doc_url:
        out.append(Suggestion(
            "Datasheet", doc_url,
            f"this URL serves a datasheet that verifies as {doc_verdict!r} "
            f"for {mpn or comp.ref}", "high" if doc_verdict == "confirmed"
            else "medium"))

    return out


def render(rows):
    """rows: [(ref, mpn, [Suggestion])] -> markdown for a human to work through."""
    L = ["# Suggested BOM fields", "",
         "The schematic should be the source of truth for Manufacturer, MPN and",
         "datasheet URL. Nothing here has been written to the design — accept,",
         "correct, or ignore each line.", ""]
    need_human = [r for r in rows
                  if any(s.confidence == "none" for s in r[2])]
    fillable = [r for r in rows if r not in need_human]

    if fillable:
        L += ["## Ready to accept", "",
              "| ref | part | field | suggested value | why | confidence |",
              "|---|---|---|---|---|---|"]
        for ref, mpn, sugg in fillable:
            for s in sugg:
                L.append(f"| {ref} | `{mpn or '?'}` | {s.field} | "
                         f"`{s.value}` | {s.why} | {s.confidence} |")
        L.append("")
    if need_human:
        L += ["## Needs a human", "",
              "No evidence was found for these; a guess would be worse than the "
              "gap.", "",
              "| ref | part | field | why |", "|---|---|---|---|"]
        for ref, mpn, sugg in need_human:
            for s in sugg:
                if s.confidence == "none":
                    L.append(f"| {ref} | `{mpn or '?'}` | {s.field} | {s.why} |")
        L.append("")
    return "\n".join(L) + "\n"
