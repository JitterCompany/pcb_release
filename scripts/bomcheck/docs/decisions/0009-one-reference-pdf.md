# ADR-0009: One real datasheet PDF in the repo for the full chain

## Status
**Current Status**: Accepted

**Date**: 2026-11-25

## Context
The reading tests start from saved `pdftotext` output. A change of PDF reader,
its version or its flags would not show there. The earlier PDF tests needed a
board folder outside the repo and never ran.

## Decision
- `tests/fixtures/pdf/tmux1309a.pdf`, TI's TMUX13xxA datasheet (SCDS477A),
  unaltered, as TI permits for its data sheets.
- A test reads it all the way: PDF to text, the verdict for five MPNs, and both
  parts' pin tables, compared with tables typed by a person.
- The tests need `pdftotext`, and fail if it is missing.

## Consequences
- 2.9 MB in the repo.
- A broken PDF-to-text step fails the tests. Checked by turning off
  `pdftotext -layout`: the TMUX1308A table then comes out wrong.

## Alternatives Considered
- **Download the PDF in the test.** Rejected: needs network, and the vendor URL
  can change or start serving a new revision.
- **A trimmed PDF with only the pin pages.** Rejected: TI permits reproduction
  only without alteration.
- **Real PDFs from a board, behind an environment variable.** Rejected: it never
  ran.
