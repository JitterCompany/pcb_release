#!/usr/bin/env python3
"""Legacy DNP/DNI migration guard.

Flags schematic symbols carrying a LEGACY do-not-populate marker -- value
DNI*/DNP/LOGO/mousebite/inf, or a non-empty dni/dnp field/property (any case,
instance OR library-inherited) -- that lack a NATIVE KiCad flag (dnp /
exclude_from_bom). Such a part would silently reappear in a BOM / pick&place made
with native --exclude-dnp, i.e. get placed though it was meant to be omitted.
Fix: set the right native flag (dnp for an unfitted part; exclude_from_bom for a
non-part like a logo or test point) so native handling == the legacy intent.

Detection is a proven SUPERSET of Kicad_bom_sync's -- see internal/kicad_netlist.py.

Usage:
  dnp_lint.py SCHEMATIC.kicad_sch      # netlist auto-exported via kicad-cli
  dnp_lint.py --netlist net.xml        # use a pre-exported kicadxml netlist
"""
import argparse
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kicad_netlist as K


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("schematic", nargs="?", help="*.kicad_sch (netlist auto-exported via kicad-cli)")
    ap.add_argument("--netlist", help="pre-exported kicadxml netlist (skips the kicad-cli export)")
    a = ap.parse_args()

    xml, tmp = a.netlist, None
    if not xml:
        if not a.schematic:
            sys.exit("dnp-lint: give a SCHEMATIC or --netlist")
        fd, xml = tempfile.mkstemp(prefix="dnplint_", suffix=".xml"); os.close(fd); tmp = xml
        subprocess.run(["kicad-cli", "sch", "export", "netlist", "--format", "kicadxml",
                        "-o", xml, a.schematic], check=True)

    comps = K.parse(xml)
    if tmp:
        os.unlink(tmp)

    viol = []
    for c in comps:
        reason = K.legacy_dnp_reason(c)
        if not reason:
            continue
        dnp, efb, _ = K.native_flags(c)
        if not (dnp or efb):
            flag = "exclude_from_bom" if c.value.lower() in ("logo", "mousebite", "inf") else "dnp"
            viol.append((c.ref, flag, reason))

    for ref, flag, why in sorted(viol):
        print(f"::error::[dnp-lint] {ref}: legacy marker ({why}) but no native flag "
              f"-- set KiCad '{flag}' (or drop the legacy marker)")
    print(f"[dnp-lint] {len(comps)} symbols, {len(viol)} legacy-only marker(s)")
    return 1 if viol else 0


if __name__ == "__main__":
    sys.exit(main())
