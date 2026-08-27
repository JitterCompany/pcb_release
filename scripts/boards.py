#!/usr/bin/env python3
"""Parse, validate and emit a project's board list.

The board list is the ONLY thing a consumer repo has to state. Everything else
(which gates exist, what they do, how they are reported) belongs here, so that a
`git pull` in the submodule is enough to pick up improvements.

Input, one board per line, policy optional and in any order:

    hardware/my-board
    hardware/my-board/my-module          skip=pinmap
    hardware/my-old-board                skip=pinmap todo=drc,3d,drift

Every gate is ENFORCED by default. That is deliberate: a new board cannot go
silently unchecked, and you cannot forget to list a gate, only exempt one.

    (unlisted)  enforced, a failure fails the build
    skip=       structurally impossible here, e.g. no MCU means no pin map.
                Skipped silently, because there is nothing to do about it.
    todo=       applies, just not green yet. Skipped, but warned about on every
                run so it stays visible.

Usage:
    boards.py --stdin                    # list on stdin
    boards.py --dir hardware/x           # a single board

Output:
    --format json   {"include": [...]}   for `strategy.matrix: fromJSON(...)`
    --format lines  dir|skip|todo        one board per line, for a shell loop.
                    '|' and not tab or space: skip may be empty while todo is not,
                    and IFS-whitespace collapses runs of delimiters, so an empty
                    middle field would silently shift todo into skip.
"""
import argparse
import json
import sys

# The gate vocabulary a board list may name. `pinmap` covers both pinmap gates:
# they are one concern to a board owner, and splitting them here would only
# invite listing one and forgetting the other.
GATES = ("erc", "drc", "3d", "drift", "pinmap")


def parse_list(text):
    """-> [ {dir, skip, todo} ].  Raises ValueError with a usable message."""
    out, seen = [], set()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        entry = {"dir": parts[0].rstrip("/"), "skip": [], "todo": []}
        if entry["dir"] in seen:
            raise ValueError(f"line {lineno}: '{entry['dir']}' listed twice")
        seen.add(entry["dir"])
        for tok in parts[1:]:
            if "=" not in tok:
                raise ValueError(
                    f"line {lineno}: expected skip=... or todo=..., got '{tok}'")
            key, val = tok.split("=", 1)
            if key not in ("skip", "todo"):
                raise ValueError(
                    f"line {lineno}: unknown field '{key}=' (expected skip= or todo=)")
            for g in (v.strip() for v in val.split(",")):
                if not g:
                    continue
                if g not in GATES:
                    raise ValueError(
                        f"line {lineno}: '{g}' is not a gate. Known: {', '.join(GATES)}")
                entry[key].append(g)
        both = set(entry["skip"]) & set(entry["todo"])
        if both:
            raise ValueError(
                f"line {lineno}: {', '.join(sorted(both))} in BOTH skip= and todo=")
        out.append(entry)
    if not out:
        raise ValueError("board list is empty")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--stdin", action="store_true", help="read the list from stdin")
    g.add_argument("--dir", help="a single board")
    ap.add_argument("--format", choices=("json", "lines"), default="json")
    a = ap.parse_args()

    text = sys.stdin.read() if a.stdin else a.dir
    try:
        boards = parse_list(text)
    except ValueError as e:
        # ::error:: so it lands on the PR: a typo here silently unenforces a gate,
        # which is the one failure this whole format exists to prevent.
        print(f"::error::[boards] {e}", file=sys.stderr)
        return 2

    # `name` is only the matrix label in the checks UI. The board's real name is
    # derived from its .kicad_pro (and [board] name in release.toml) downstream.
    for b in boards:
        b["name"] = b["dir"].rstrip("/").split("/")[-1]
        b["skip"] = ",".join(b["skip"])
        b["todo"] = ",".join(b["todo"])
    if a.format == "lines":
        for b in boards:
            print("|".join((b["dir"], b["skip"], b["todo"])))
    else:
        print(json.dumps({"include": boards}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
