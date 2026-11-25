# ADR-0010: The terminal and the web page are two front ends for the same actions

## Status
**Current Status**: Accepted

**Date**: 2026-11-25

## Context
The wizard runs in a terminal or, with `--gui`, in a browser tab beside the
datasheet page. Commands typed in the terminal (`:n` numbers only, `-` not
connected, `:s` skip) had no button in the page, so the page could not do
everything.

## Decision
- One `Wizard` asks the questions. The terminal and the page only draw them.
- Every command a terminal user can type is also a button where it applies. A
  new command gets its button in the same change.

## Consequences
- The page and the terminal stay equal without a second implementation.
- Subcommands outside the wizard (status, prepare, apply) have no page yet.
