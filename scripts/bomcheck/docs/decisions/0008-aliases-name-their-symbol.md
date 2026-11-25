# ADR-0008: Aliases name the symbols they were accepted for

## Status
**Current Status**: Accepted

**Date**: 2026-11-25

## Context
An alias accepts a symbol's pin name as equal to the datasheet's (`VCC` for
`VDD`). The alias is a claim about a part on one symbol, but the table is keyed
to the part. On another symbol the same alias can accept a wrong name.

## Decision
- New alias blocks (`[[variant.alias_for]]`) list the symbols they were accepted
  for. On another symbol they do not apply, and bomcheck reports
  `alias_other_symbol`.
- The wizard then asks whether the new symbol's pinout is the same. If yes, the
  symbol is added to the block. If not, the person goes pin by pin, and the new
  symbol gets its own block.
- Old `[variant.symbol_alias]` tables keep their meaning: they apply to every
  symbol (ADR-0006).

## Consequences
- Reusing a part with a different symbol is checked again, once.
- Old tables get this protection only when someone converts them. No command
  does that yet.

## Alternatives Considered
- **Make `symbol_alias` symbol-scoped.** Rejected: it changes the meaning of
  signed files.
