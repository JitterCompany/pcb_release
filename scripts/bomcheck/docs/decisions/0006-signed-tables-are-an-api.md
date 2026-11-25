# ADR-0006: Signed tables are an API, guarded by one table per format feature

## Status
**Current Status**: Accepted

**Date**: 2026-11-25

## Context
A signed table records a person's check. A board released with it must keep
passing when bomcheck changes, or CI fails a design that did not change.
A board pins its pcb_release version, but its component library can be newer.

## Decision
- The format only grows. Existing keys keep their meaning, new keys are
  optional, finding and waiver codes are stable names. The rules are in
  FORMAT.md, "Compatibility".
- An older bomcheck reading a newer table may check less, but must not fail.
- `tests/fixtures/compat/` holds real signed tables, one per format feature or
  naming rule, with trimmed board netlists that use them. A test requires each
  board to stay fully checked. These files are never edited. A new format
  feature adds one real table that uses it.

## Consequences
- A format change that breaks an existing table fails the tests.
- A new feature cannot land without a real table that uses it, once one exists.

## Alternatives Considered
- **A dated copy of every signed table per format change.** Rejected: it grows
  with every change, and ten tables with the same structure test nothing more
  than one.
- **No fixtures, rely on the unit tests.** Rejected: unit tests use tables the
  tests wrote, not tables people signed.
