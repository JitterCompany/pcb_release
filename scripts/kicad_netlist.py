#!/usr/bin/env python3
"""Minimal KiCad kicadxml-netlist reader (MIT, no external deps).

Just enough to classify components for the DNP / placement checks: value, all
fields and properties (names + values, case preserved), and KiCad's native flags.

SAFETY (why this can't silently miss a do-not-populate part):
  * legacy DNP/DNI detection scans BOTH <field> AND <property> -- KiCad resolves
    library-inherited fields into <property>, so a marker set on the symbol
    library (not the instance) is still seen;
  * dni/dnp names are matched CASE-INSENSITIVELY -- a mis-cased "Dnp" is caught;
  * it is deliberately a SUPERSET of Kicad_bom_sync's detection. A false positive
    is harmless (you just set a native flag); a miss would place an unpopulated
    part, so we never risk one.
Native flags use KiCad's exact reserved lowercase property names.
"""
import xml.etree.ElementTree as ET

NATIVE_DNP = "dnp"
NATIVE_EXCLUDE_BOM = "exclude_from_bom"
NATIVE_EXCLUDE_POS = "exclude_from_pos_files"
LEGACY_VALUE_EXACT = {"dni", "dnp", "logo", "mousebite", "inf"}   # Kicad_bom_sync BOM.py
LEGACY_FIELD_NAMES = {"dni", "dnp"}                               # such a field, non-empty = legacy DNP


class Comp:
    __slots__ = ("ref", "value", "fields", "props")
    def __init__(self, ref, value, fields, props):
        self.ref, self.value, self.fields, self.props = ref, value, fields, props


def parse(xml_path):
    """-> [Comp] for every <comp> in a kicad-cli 'kicadxml' netlist."""
    root = ET.parse(xml_path).getroot()
    comps = []
    for c in root.iter("comp"):
        fields = {(f.get("name") or ""): (f.text or "").strip() for f in c.findall("fields/field")}
        props = {(p.get("name") or ""): (p.get("value") or "").strip() for p in c.findall("property")}
        comps.append(Comp(c.get("ref") or "", (c.findtext("value") or "").strip(), fields, props))
    return comps


def native_flags(c):
    """(dnp, exclude_from_bom, exclude_from_pos) from KiCad's reserved properties (exact lowercase)."""
    return (NATIVE_DNP in c.props, NATIVE_EXCLUDE_BOM in c.props, NATIVE_EXCLUDE_POS in c.props)


def legacy_dnp_reason(c):
    """Human reason string if a LEGACY (pre-native) DNP/DNI marker is present, else None.
    Superset-safe: case-insensitive over value + all fields + all properties."""
    vl = c.value.lower()
    if vl.startswith("dni") or vl in LEGACY_VALUE_EXACT:
        return f"value={c.value!r}"
    for name, val in list(c.fields.items()) + list(c.props.items()):
        if name.strip().lower() in LEGACY_FIELD_NAMES and val.strip():   # non-empty dni/dnp field
            return f"field {name}={val!r}"
    return None


def not_placed(c):
    """True if the part must NOT be placed: a native flag OR a legacy DNP marker."""
    dnp, efb, efp = native_flags(c)
    return dnp or efb or efp or (legacy_dnp_reason(c) is not None)
