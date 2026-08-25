#!/usr/bin/env python3
"""Tally a kicad-cli ERC/DRC JSON report and apply a pass/fail policy.

Schema-tolerant: recursively finds violation objects (any dict carrying a
string "severity") so the same parser works for `sch erc` and `pcb drc`
output, including the DRC unconnected-items and schematic-parity sections.

Policy:
    errors always fail; warnings fail only with --strict.
    Violations that KiCad marks excluded (per-item "excluded": true, set in the
    GUI and stored in the design files) never gate and are NOT listed — they are
    only tallied as "excluded=Q", so CI mirrors what you see in KiCad. kicad-cli
    keeps such an item's real severity and flags it with "excluded", so we must
    classify on that flag, not on the severity string.

Exit code: 0 = pass, 1 = fail (2 = bad usage / unreadable report).
"""
import argparse
import json
import sys
from collections import Counter

# Severities that a violation can carry. Only error/warning ever gate.
GATING = ("error", "warning")


def find_violations(node, out):
    """Collect every dict that has a string "severity" field. Do not descend
    into a violation's own sub-items once counted, so offending pads/pins that
    might carry their own severity can't double-count the parent violation."""
    if isinstance(node, dict):
        if isinstance(node.get("severity"), str):
            out.append(node)
            return
        for value in node.values():
            find_violations(value, out)
    elif isinstance(node, list):
        for value in node:
            find_violations(value, out)


def fmt_loc(v):
    """' @ (x.xx, y.yy) mm' built from a violation's item positions, or '' if
    none — so a DRC/ERC annotation says WHERE on the board, no artifact needed."""
    pts = []
    for it in v.get("items", []):
        p = it.get("pos")
        if isinstance(p, dict) and "x" in p and "y" in p:
            pts.append(f"({p['x']:.2f}, {p['y']:.2f})")
    return f" @ {', '.join(pts)} mm" if pts else ""


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("report", help="kicad-cli JSON report file")
    ap.add_argument("--strict", action="store_true",
                    help="fail on warnings too (release gate)")
    ap.add_argument("--label", default="check",
                    help="label for log lines, e.g. ERC or DRC")
    ap.add_argument("--ignore-type", action="append", default=[],
                    help="violation 'type' key(s) that are CI-environment noise, not "
                         "design issues (e.g. footprint_link_issues = no fp-lib table in "
                         "a headless run). Repeatable or comma-separated; counted as "
                         "'ignored', never gate, still visible in the KiCad GUI.")
    args = ap.parse_args()

    try:
        with open(args.report) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"::error::[{args.label}] cannot read report {args.report}: {e}")
        return 2

    violations = []
    find_violations(data, violations)

    ignore_types = set()
    for t in args.ignore_type:                     # repeatable and/or comma-separated
        ignore_types.update(s.strip() for s in t.split(",") if s.strip())

    # KiCad marks user-excluded items with "excluded": true (keeping their real
    # severity) — never gate, not listed, only tallied, so CI mirrors the GUI.
    # --ignore-type then drops whole violation classes that are CI-environment
    # noise (not design issues); those are counted as 'ignored', never gate.
    kept = [v for v in violations if not v.get("excluded")]
    n_excl = len(violations) - len(kept)
    ignored = [v for v in kept if v.get("type") in ignore_types]
    active = [v for v in kept if v.get("type") not in ignore_types]
    counts = Counter(v.get("severity") for v in active)
    n_err = counts.get("error", 0)
    n_warn = counts.get("warning", 0)

    # Surface each non-excluded error/warning as a GitHub annotation, with the
    # board location when present, so the checks UI says WHERE (no artifact).
    for v in active:
        sev = v.get("severity")
        if sev in GATING:
            desc = v.get("description") or v.get("type") or "(no description)"
            annot = "::error::" if sev == "error" else "::warning::"
            print(f"{annot}[{args.label}] {sev.upper()}: {desc}{fmt_loc(v)}")

    # Don't silently swallow the ignored classes — report what was dropped.
    if ignored:
        byt = Counter(v.get("type") for v in ignored)
        print(f"[{args.label}] ignored (CI-env noise): "
              + ", ".join(f"{t}={c}" for t, c in sorted(byt.items())))

    print(f"[{args.label}] summary: "
          f"errors={n_err} warnings={n_warn} excluded={n_excl} ignored={len(ignored)}")

    # No RESULT line here on purpose: pcb-release.sh prints ONE verdict per stage,
    # in one style, so nothing else claims a pass/fail. Say WHY we are about to
    # fail, though -- the caller only knows the exit code.
    fail = n_err > 0 or (args.strict and n_warn > 0)
    if fail:
        why = "errors present" if n_err else "warnings present (--strict)"
        print(f"[{args.label}] gating: {why}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
