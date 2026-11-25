# ADR-0005: tomlite instead of a TOML library

## Status
**Current Status**: Accepted

**Date**: 2026-11-25 (recorded after the fact)

## Context
The signed tables are the database, so a misread is a silent wrong result.
bomcheck uses the Python standard library only. `tomllib` is not in Python
3.10. Small ad-hoc readers that split on `,` or `#` break on quoted strings,
which these files are full of. The tool also edits the files, and must keep a
person's comments.

## Decision
- bomcheck always reads with `tomlite.py`, a reader for the TOML subset the
  format uses.
- The tests compare it with `tomllib` where that exists.
- bomcheck changes files line by line (`reffile.py`) and never regenerates
  them, so comments stay.

## Consequences
- CI and a laptop read a file the same way, on any Python version.
- The format must stay inside the subset tomlite reads.

## Alternatives Considered
- **`tomllib` when available, a fallback otherwise.** Rejected: two parsers can
  read the same file differently.
- **A pip package.** Rejected: bomcheck runs from a copy of its folder, with no
  install step.
