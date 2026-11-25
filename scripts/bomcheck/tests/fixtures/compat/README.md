# Signed tables that must keep passing

Real signed tables from KicadComponents, with the two boards that use them
(netlist trimmed to these parts, and the one symbol drawing the check needs).
Only the signer's name is replaced. `test_signed_tables_stay_valid` checks that
each board is still fully checked.

One table per format feature or naming rule, not every table we have:

| Table | Covers |
|---|---|
| TLV76733DGNR | plain pin table, typed by hand |
| TPS61022RWUT | plain pin table, read from the datasheet and signed |
| TPS25940LRVCR | read from the datasheet, with `symbol_alias` |
| BAT54ALT1G | `symbol_alias` on a 3-pin part |
| NX3008NBKSH | dual FET: pins named per symbol unit (needs `sl-modem/board.kicad_sch`) |
| SIM8051-6-0-14-01-A | SIM card contact names |
| SSW-125-01-T-D | numbers-only part (`pin_numbers`, `joined`, `pad_layout`) |
| T520B337M006ATE040 | polarity part: `mark` and `marked` |
| XGD10402KR | polarity part: `marked` only |
| SL23-SL23_Header_2x25 | interface table |

Never edit these files. When the format gains a feature, add one real table
that uses it (and its part on a board netlist), and a row here.
