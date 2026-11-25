# Decision records

Architecture Decision Records (ADRs) for bomcheck: why it works the way it
does. Each record is immutable. A new decision supersedes an old one rather
than rewriting it. Status: Proposed, Accepted, Deprecated or Superseded.

Naming: `XXXX-kebab-case-title.md` is ADR-XXXX.

| ID | Title | Status | Date |
|----|-------|--------|------|
| ADR-0001 | [The datasheet is the only reference, read without the symbol](0001-datasheet-is-the-only-reference.md) | Accepted | 2026-11-25 |
| ADR-0002 | [A datasheet must be about this part; MPNs are listed, never patterns](0002-a-datasheet-must-be-about-this-part.md) | Accepted | 2026-11-25 |
| ADR-0003 | [One document, several parts: one variant per part and package](0003-one-document-several-parts.md) | Accepted | 2026-11-25 |
| ADR-0004 | [Pin tables are read by a named set of shapes](0004-named-table-shapes.md) | Accepted | 2026-11-25 |
| ADR-0005 | [tomlite instead of a TOML library](0005-tomlite.md) | Accepted | 2026-11-25 |
| ADR-0006 | [Signed tables are an API, guarded by one table per format feature](0006-signed-tables-are-an-api.md) | Accepted | 2026-11-25 |
| ADR-0007 | [Numbers-only parts, with interface tables keyed to the symbol](0007-interface-tables.md) | Accepted | 2026-11-25 |
| ADR-0008 | [Aliases name the symbols they were accepted for](0008-aliases-name-their-symbol.md) | Accepted | 2026-11-25 |
| ADR-0009 | [One real datasheet PDF in the repo for the full chain](0009-one-reference-pdf.md) | Accepted | 2026-11-25 |
| ADR-0010 | [The terminal and the web page are two front ends for the same actions](0010-two-front-ends.md) | Accepted | 2026-11-25 |
| ADR-0011 | [Overwriting a schematic value needs its own flag](0011-writes-to-the-schematic.md) | Accepted | 2026-11-25 |
