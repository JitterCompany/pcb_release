#!/usr/bin/env python3
"""Parts excused from the datasheet rule, in attestations/exemptions.toml.

Every BOM line needs a datasheet the schematic points at, unless a person
recorded why it cannot have one. The file is hand-editable; the tool only ever
appends to it.
"""
import os

import atomic
import tomlite

REASONS = {
    "confidential": "under NDA, may not be shipped with the repo",
    "too_large": "too large to keep in the repo",
    "none_exists": "the manufacturer publishes no datasheet",
    "other": "see the note",
}

HEADER = """# Parts excused from the "every BOM line has a datasheet" rule.
# reason is one of: {reasons}
# An exempt part's pin table is still checked. With no readable PDF it is
# typed and signed by hand.
"""


class Exemption:
    __slots__ = ("mpn", "reason", "note", "by", "on")

    def __init__(self, d):
        self.mpn = str(d.get("mpn", "")).strip()
        self.reason = str(d.get("reason", "")).strip()
        self.note = str(d.get("note", "")).strip()
        self.by = str(d.get("by", "")).strip()
        self.on = str(d.get("on", "")).strip()

    @property
    def problem(self):
        if self.reason not in REASONS:
            return f"{self.mpn}: reason {self.reason!r} is not one of {', '.join(REASONS)}"
        if not self.by:
            return f"{self.mpn}: exemption has no `by`, so nobody made it"
        return ""


def path_of(att):
    return os.path.join(att, "exemptions.toml")


def load(att):
    """-> {mpn: Exemption}. Invalid entries are kept; callers check .problem."""
    path = path_of(att)
    if not os.path.isfile(path):
        return {}
    data = tomlite.load(path)
    return {e.mpn: e for e in (Exemption(d) for d in data.get("datasheet_exempt") or [])}


def add(att, mpn, reason, note, by, on):
    if reason not in REASONS:
        raise ValueError(f"unknown reason {reason!r}")
    path = path_of(att)
    text = (open(path, encoding="utf-8").read() if os.path.isfile(path)
            else HEADER.format(reasons=", ".join(REASONS)))
    entry = ["", "[[datasheet_exempt]]",
             f"mpn    = {tomlite.quote(mpn)}",
             f"reason = {tomlite.quote(reason)}"]
    if note:
        entry.append(f"note   = {tomlite.quote(note)}")
    entry += [f"by     = {tomlite.quote(by)}", f"on     = {tomlite.quote(on)}"]
    atomic.write_text(path, text.rstrip("\n") + "\n" + "\n".join(entry) + "\n")
