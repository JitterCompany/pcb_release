# Reference PDF

`tmux1309a.pdf`: Texas Instruments TMUX13xxA datasheet, SCDS477A (Rev. A),
51 pages, from https://www.ti.com/lit/ds/symlink/tmux1309a.pdf.
sha256 `f7c7ee336fa1da1e6076ca7b9dda2b433fed4b4b3d44b9546b0dd1e1af0eb0d6`.

Kept unaltered, as TI permits for its data sheets. It is the one test of the
whole chain from PDF to pin table (`test_reference_pdf_to_pin_table`). The
`.txt` fixtures start after the PDF reader; this one starts before it.

The document covers two parts with different pinouts (TMUX1308A, TMUX1309A),
so it also tests that each part number gets its own table.

Do not replace it with a newer revision: the expected tables in the test are
read from this one.
