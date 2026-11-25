#!/usr/bin/env python3
"""tomlite regression suite.

Every case in CASES is a value that a naive TOML reader gets wrong (a '#' or a
',' inside a quoted string, arrays of tables, and so on), taken from the kind
of data the signed tables hold. If a case here fails, a signed table can be
silently misread.

Where tomllib is available (py>=3.11) it is run as a differential oracle: our
result must equal the real parser's, so this suite proves agreement with the
spec rather than mere self-consistency.

Usage: python3 tests/test_tomlite.py [-v]        (exit 0 pass, 1 fail)
"""
import datetime
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tomlite

# (name, toml source, expected dict) -- expected is checked EXACTLY.
CASES = [
    (
        "array with commas inside quoted strings (BOM line 17, real value)",
        'rating = ["600W, Vr=13V,Vc=22V", "Cj<1nF"]\n',
        {"rating": ["600W, Vr=13V,Vc=22V", "Cj<1nF"]},
    ),
    (
        "hash inside a quoted string is data, not a comment",
        'claim = "pad 1 = pin 1 = dot corner # 1, CCW"\n',
        {"claim": "pad 1 = pin 1 = dot corner # 1, CCW"},
    ),
    (
        "a real comment is still stripped",
        'name = "TMUX1309APWR"   # the fitted part\n',
        {"name": "TMUX1309APWR"},
    ),
    (
        "non-ASCII survives (TI section marks, ohm signs, euro)",
        'source = "TI SCDS398C §9.1, 2.5 Ω Ron, €0.61"\n',
        {"source": "TI SCDS398C §9.1, 2.5 Ω Ron, €0.61"},
    ),
    (
        "bare date -> date object, not int or string",
        "on = 2026-08-21\n",
        {"on": datetime.date(2026, 8, 21)},
    ),
    (
        "three-level array-of-tables tree (the attestation schema)",
        '[[symbol]]\n'
        'name = "Jitter_IC:TMUX1309A"\n'
        '  [[symbol.footprint]]\n'
        '  name = "Package_SO:TSSOP-16_4.4x5mm_P0.65mm"\n'
        '    [[symbol.footprint.part]]\n'
        '    mpn = "TMUX1309APWR"\n'
        '    rating = []\n',
        {"symbol": [{"name": "Jitter_IC:TMUX1309A",
                     "footprint": [{"name": "Package_SO:TSSOP-16_4.4x5mm_P0.65mm",
                                    "part": [{"mpn": "TMUX1309APWR", "rating": []}]}]}]},
    ),
    (
        "second [[symbol]] does not leak into the first",
        '[[symbol]]\nname = "A"\n  [[symbol.footprint]]\n  name = "fpA"\n'
        '[[symbol]]\nname = "B"\n  [[symbol.footprint]]\n  name = "fpB"\n',
        {"symbol": [{"name": "A", "footprint": [{"name": "fpA"}]},
                    {"name": "B", "footprint": [{"name": "fpB"}]}]},
    ),
    (
        "multi-line array (proposals wrap long mpns lists)",
        'mpns = [\n  "TMUX1309APWR",   # tape and reel\n  "TMUX1309APWT",\n]\n',
        {"mpns": ["TMUX1309APWR", "TMUX1309APWT"]},
    ),
    (
        "inline table (ordering_check records)",
        '[variant.ordering_check]\n'
        'TMUX1309APWR = { found = true, table = "Table 11-1" }\n',
        {"variant": {"ordering_check": {"TMUX1309APWR":
                                        {"found": True, "table": "Table 11-1"}}}},
    ),
    (
        "triple-quoted evidence string with newlines and a hash",
        'evidence = """\nSCDS398C Table 6-1 lists pin 8 = GND.\n'
        'Symbol #2 disagrees.\n"""\n',
        {"evidence": "SCDS398C Table 6-1 lists pin 8 = GND.\nSymbol #2 disagrees.\n"},
    ),
    (
        "escaped quote inside a basic string",
        'note = "the \\"A\\" variant"\n',
        {"note": 'the "A" variant'},
    ),
    (
        "literal string keeps backslashes (windows-ish paths, regex)",
        "pat = 'RC\\d{4}[FJ]R'\n",
        {"pat": "RC\\d{4}[FJ]R"},
    ),
    (
        "numeric-looking pin keys stay usable as keys",
        '[pins]\n1 = "A0"\n16 = "VDD"\n',
        {"pins": {"1": "A0", "16": "VDD"}},
    ),
    (
        "floats, ints, bools, empty array",
        "cap_uF = 5.0\nmin = 1\nrohs = true\nrating = []\n",
        {"cap_uF": 5.0, "min": 1, "rohs": True, "rating": []},
    ),
    (
        "dotted key",
        'meta.version = 1\n',
        {"meta": {"version": 1}},
    ),
]

# Sources that MUST be rejected with a line number rather than silently guessed.
REJECT = [
    ("unterminated string", 'a = "oops\n'),
    ("duplicate key", 'a = 1\na = 2\n'),
    ("missing equals", 'a 1\n'),
    ("unterminated array", 'a = [1, 2\n'),
    ("garbage value", 'a = @@@\n'),
]


def main():
    verbose = "-v" in sys.argv
    fails = []

    try:
        import tomllib
        oracle = tomllib.loads
    except ImportError:
        oracle = None

    for name, src, want in CASES:
        try:
            got = tomlite.loads(src)
        except Exception as e:                                  # noqa: BLE001
            fails.append(f"{name}: raised {e!r}")
            continue
        if got != want:
            fails.append(f"{name}:\n     got  {got!r}\n     want {want!r}")
            continue
        if oracle:
            try:
                ref = oracle(src)
            except Exception:                                   # noqa: BLE001
                ref = None                                      # our superset is fine
            if ref is not None and ref != got:
                fails.append(f"{name}: disagrees with tomllib\n"
                             f"     ours    {got!r}\n     tomllib {ref!r}")
                continue
        if verbose:
            print(f"  ok   {name}")

    for name, src in REJECT:
        try:
            tomlite.loads(src)
        except tomlite.TomlError as e:
            if "line" not in str(e):
                fails.append(f"reject/{name}: error lacks a line number: {e}")
            elif verbose:
                print(f"  ok   reject: {name}  ({e})")
        except Exception as e:                                  # noqa: BLE001
            fails.append(f"reject/{name}: wrong exception type {e!r}")
        else:
            fails.append(f"reject/{name}: accepted invalid input")

    total = len(CASES) + len(REJECT)
    if fails:
        for f in fails:
            print(f"FAIL {f}")
        print(f"\ntomlite: {total - len(fails)}/{total} passed, {len(fails)} FAILED")
        return 1
    print(f"tomlite: {total}/{total} passed"
          f"{' (differentially vs tomllib)' if oracle else ' (no tomllib oracle on this python)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
