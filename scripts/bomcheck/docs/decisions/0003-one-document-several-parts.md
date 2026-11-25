# ADR-0003: One document, several parts: one variant per part and package

## Status
**Current Status**: Accepted

**Date**: 2026-11-25 (recorded after the fact)

## Context
A datasheet often covers a family with different pinouts in the same package
(TMUX1308A and TMUX1309A share one document and one package). Reading the wrong
part's table gives a complete, confident, wrong result.

## Decision
A table file is one document. It holds one `[[variant]]` per part and package,
and the MPN selects the variant. Reading scopes to the MPN's own table in the
document.

## Consequences
- One signed file serves every part the document describes.
- Reading must find the part's own section, so the reader needs to understand
  headings that name a part.

## Alternatives Considered
- **One file per MPN.** Rejected: the same document would be signed several
  times, and the files drift apart.
