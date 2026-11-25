# ADR-0004: Pin tables are read by a named set of shapes

## Status
**Current Status**: Accepted

**Date**: 2026-11-25 (recorded after the fact)

## Context
Vendors reuse a few pin-table layouts: a row per pin, a name with a list of
pins, a column per package, a pinout drawing. Each vendor adds quirks, such as
long names split over two lines.

## Decision
- `tables.py` names each layout as a shape. One driver applies them in order.
- A new quirk is a new shape or a fix inside one shape, with a regression test
  on the saved page text (`tests/fixtures/*.txt`). The test's docstring names
  the vendor quirk.
- A table with more pins than the part has is refused: it looks finished and
  is not.

## Consequences
- A fix for one vendor helps every part with the same layout.
- The saved page text is the regression suite for reading. One real PDF tests
  the step before it (ADR-0009).

## Alternatives Considered
- **One reader per vendor.** Rejected: vendors share layouts, so fixes would be
  copied around.
