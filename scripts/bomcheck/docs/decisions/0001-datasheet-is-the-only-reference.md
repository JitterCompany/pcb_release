# ADR-0001: The datasheet is the only reference, read without the symbol

## Status
**Current Status**: Accepted

**Date**: 2026-11-25 (recorded after the fact; the design dates from the first version)

## Context
bomcheck exists to catch a symbol whose pin table does not match the part. Any
evidence that can be steered by the symbol agrees with a wrong symbol.

KiCad's own symbol libraries were tried as a second reference. They matched
almost no MPNs on their own, and matching by name prefix joined different parts.

## Decision
- The only reference is the part's datasheet.
- Reading a datasheet (stage one) gets the MPN and the document, nothing from
  the symbol. `tables.Context` has no symbol field.
- The expected pin count comes from the order code or the footprint's package
  size, never from the symbol. A package name that is not a pin count
  (`SOT-353`) gives "unknown": a wrong expectation is worse than none.
- Names in a table are the datasheet's. A stub leaves names blank, so nothing
  is copied from the symbol.
- When one table covers several packages, the column comes from the package the
  MPN decodes to, or from the footprint. Never the column that fits the symbol
  best.
- Naming conventions (`VDD` vs `VCC`, SPI roles) belong to the comparison
  (stage two), not to reading. A test checks that the reading modules contain
  no convention code.

## Consequences
- A symbol drawn for the wrong part is caught, even when its pin count matches.
- Every part needs a datasheet and a person to sign its table once.
- No symbol library is used as evidence, so library symbols get no shortcut.

## Alternatives Considered
- **KiCad's symbol libraries as a second reference.** Rejected: few MPNs
  matched, and prefix matching joined different parts.
- **Pick the table column that fits the symbol.** Rejected: any symbol passes.
