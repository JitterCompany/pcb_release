# ADR-0002: A datasheet must be about this part; MPNs are listed, never patterns

## Status
**Current Status**: Accepted

**Date**: 2026-11-25 (recorded after the fact)

## Context
A file in a Datasheet field proves nothing. Wrong documents happen: a B-variant
datasheet next to the non-B part, a module family document next to another
module. Two order codes can differ by one character and differ in function (a
uni- and a bidirectional TVS diode).

## Decision
- bomcheck checks that the document names the order code as a whole token
  (`ADS114S08` is not `ADS114S08B`).
- A document that names only the family is `family-only` and needs a person.
- A document that names another part far more often than ours is rejected.
- A table lists its MPNs exactly, or names a decoder that is implemented in this
  folder. No patterns or code in data files.

## Consequences
- A wrong document is caught before its table is read.
- A data file can never make CI run code.
- Each new order code of a family is added to `mpns` by hand.

## Alternatives Considered
- **Substring or prefix match on the MPN.** Rejected: `ADS114S08` matches inside
  `ADS114S08B`.
- **MPN patterns in the table file.** Rejected: a one-character difference can
  be a different part, and a pattern language in data is code.
