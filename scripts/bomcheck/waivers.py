#!/usr/bin/env python3
"""Design findings a person decided not to fix now, in attestations/waivers.toml.

    [[waiver]]
    code    = "polarity.bidirectional_on_marked"   # stable finding code
    refs    = ["D19"]
    pins    = ["7"]      # optional, pin differences only
    for_rev = "2.0"      # optional: only while the design is exactly rev 2.0
    about   = "..."      # written by the tool: what it was about, for readers
    note    = "..."      # required: why
    by      = "..."      # required: who
    on      = "2026-09-30"

Without `for_rev` a waiver accepts the finding for good. With it, the finding is
a known issue of that revision: when the schematic's revision changes, it fails
again until it is fixed or waived again on purpose.

A waiver is matched on its code, refs and pins, never on a message: messages
get reworded, codes do not. A waiver that matches nothing any more fails too,
so the file cannot quietly fill up with waivers for things long fixed.
"""
import os

import atomic
import tomlite

HEADER = ("# Design findings accepted for good, or known issues of one revision.\n"
          "# for_rev: the waiver holds only while the schematic's revision is\n"
          "# exactly that value; any other revision fails it again. No for_rev:\n"
          "# accepted for good. about: context only, never used for matching.\n"
          "# Format: scripts/bomcheck/FORMAT.md.\n")


class Waiver:
    __slots__ = ("code", "refs", "pins", "until", "about", "note", "by", "on",
                 "used")

    def __init__(self, d):
        self.code = str(d.get("code", "")).strip()
        self.refs = [str(r).strip() for r in d.get("refs") or []]
        self.pins = [str(p).strip() for p in d.get("pins") or []]
        self.until = str(d.get("for_rev", "")).strip()
        self.about = str(d.get("about", "")).strip()
        self.note = str(d.get("note", "")).strip()
        self.by = str(d.get("by", "")).strip()
        self.on = str(d.get("on", "")).strip()
        self.used = False

    def label(self):
        """Refs and what was accepted, in words when `about` says it."""
        where = " ".join(self.refs) + (f" pin {' '.join(self.pins)}"
                                       if self.pins else "")
        return f"{where} ({self.about or self.code})"


def path_of(att):
    return os.path.join(att, "waivers.toml")


def load(att):
    path = path_of(att)
    if not os.path.isfile(path):
        return []
    return [Waiver(d) for d in tomlite.load(path).get("waiver") or []]


def add(att, code, refs, note, by, on, pins=(), until="", about=""):
    path = path_of(att)
    text = open(path, encoding="utf-8").read() if os.path.isfile(path) else HEADER
    q = tomlite.quote
    entry = ["", "[[waiver]]", f"code    = {q(code)}",
             f"refs    = [{', '.join(q(r) for r in refs)}]"]
    if pins:
        entry.append(f"pins    = [{', '.join(q(p) for p in pins)}]")
    if until:
        entry.append(f"for_rev = {q(until)}")
    if about:
        entry.append(f"about   = {q(about)}")
    entry += [f"note    = {q(note)}", f"by      = {q(by)}", f"on      = {q(on)}"]
    atomic.write_text(path, text.rstrip("\n") + "\n" + "\n".join(entry) + "\n")


def _blocks(text):
    """-> (head, [entry text]) split on [[waiver]]."""
    parts = text.split("\n[[waiver]]")
    return parts[0], ["[[waiver]]" + p for p in parts[1:]]


def _is(entry, w):
    d = Waiver(tomlite.loads(entry).get("waiver", [{}])[0])
    return (d.code, d.refs, d.pins, d.until) == (w.code, w.refs, w.pins, w.until)


def _rewrite(att, w, change):
    path = path_of(att)
    head, entries = _blocks(open(path, encoding="utf-8").read())
    out = [change(e) if _is(e, w) else e for e in entries]
    text = head.rstrip("\n") + "\n" + "".join(
        "\n" + e.strip("\n") + "\n" for e in out if e)
    atomic.write_text(path, text)


def prune(att, stale):
    """Remove waivers that match no finding any more. -> the removed ones."""
    for w in stale:
        remove(att, w)
    return stale


def remove(att, w):
    _rewrite(att, w, lambda e: "")


def renew(att, w, rev, by, on):
    """Carry a known issue over to another revision, on purpose."""
    import re

    def change(e):
        e = re.sub(r'^for_rev\s*=.*$', f"for_rev = {tomlite.quote(rev)}", e, flags=re.M)
        e = re.sub(r'^by\s*=.*$', f"by      = {tomlite.quote(by)}", e, flags=re.M)
        return re.sub(r'^on\s*=.*$', f"on      = {tomlite.quote(on)}", e, flags=re.M)
    _rewrite(att, w, change)


def apply(items, waivers, rev, waivable, make_item):
    """-> (open items, [(item, waiver)] waived).

    Problems with the waivers themselves come back as open items, so they fail
    like any finding. `make_item(kind, waiver, detail)` builds those.
    """
    open_items, waived = [], []
    usable = []
    for w in waivers:
        if w.code not in waivable:
            open_items.append(make_item("waiver_invalid", w, 
                                        f"{w.label()}: {w.code!r} cannot be waived"))
        elif not w.note or not w.by:
            open_items.append(make_item("waiver_invalid", w, 
                                        f"{w.label()}: needs a note and a name (by)"))
        elif w.until and w.until != rev:
            w.used = True
            open_items.append(make_item(
                "waiver_expired", w,
                f"{w.label()} was a known issue of rev {w.until}; the design is "
                f"rev {rev or '(unknown)'}: fix it, or accept it again for this rev"))
        else:
            usable.append(w)

    for it in items:
        rest = _apply_one(it, [w for w in usable if w.code == it.code], waived)
        if rest is not None:
            open_items.append(rest)

    for w in usable:
        if not w.used:
            open_items.append(make_item("waiver_stale", w,
                                        f"{w.label()} no longer occurs; "
                                        f"remove it from waivers.toml"))
    return open_items, waived


def _apply_one(it, candidates, waived):
    """Waive what the candidates cover; return what is left open, or None."""
    for w in candidates:
        covered = [r for r in it.refs if r in w.refs]
        if not covered:
            continue
        if it.findings and w.pins:
            hit = [f for f in it.findings if f.get("pin") in w.pins]
            if not hit:
                continue
            w.used = True
            it.findings = [f for f in it.findings if f not in hit]
            waived.append((it, w))
            if not it.findings:
                return None
            continue
        w.used = True
        waived.append((it, w))
        rest = [r for r in it.refs if r not in covered]
        if not rest:
            return None
        it.refs = rest
    return it
