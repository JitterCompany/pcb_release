# ADR-0011: Overwriting a schematic value needs its own flag

## Status
**Current Status**: Accepted

**Date**: 2026-11-25 (recorded after the fact)

## Context
`apply` writes field values (MPN, Manufacturer, Datasheet) into the schematic.
Filling an empty field is easy to undo. Overwriting a value an engineer chose
is not. Accepting a pin difference is a claim about the design.

## Decision
- A row that overwrites a value (`REPLACE`) is carried out only with
  `bomcheck apply --replace`.
- The `accept` column of the pin review starts empty. A person fills it.

## Consequences
- A default run never changes an engineer's value.
- The layout needs Update PCB from Schematic (F8) after `apply`.
