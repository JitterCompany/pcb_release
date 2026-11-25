# ADR-0007: Numbers-only parts, with interface tables keyed to the symbol

## Status
**Current Status**: Accepted

**Date**: 2026-11-25

## Context
A plain connector's datasheet numbers its contacts and names none of them. Its
symbol often names the pins after an interface spec (the SL23 2x25 header). The
first such table carried the SL23 names as aliases in the socket's part table.
That table is keyed to the MPN, so another board with the same socket and
another symbol would have been checked against SL23 names.

## Decision
- The part table records only the numbering (`pin_numbers`), from the part's
  datasheet.
- The names come from an interface table, keyed to the symbols that follow the
  spec, typed from the spec.
- The symbol names its interface in the field `Pinout_Spec`. Not `Interface`:
  the component library already uses that field for a sensor's bus.
- While a person types an interface table, the wizard shows the spec, not the
  symbol's names.

## Consequences
- One interface table checks every board that uses that symbol, whatever socket
  is fitted.
- The symbol needs the `Pinout_Spec` field. bomcheck does not edit libraries, so
  a person adds it.
- A part's Datasheet field must be the part's datasheet, not the spec.

## Alternatives Considered
- **Aliases in the part table.** Rejected: keyed to the MPN, not the symbol.
- **One interface table per interface, for all symbols.** Rejected: the FFC and
  the header of one interface have different pin numbers.
