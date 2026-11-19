#!/usr/bin/env python3
"""Edit a reference pin table in place, as text.

These files are the database: people read and edit them by hand. So the tool
changes only the lines it means to change, and never re-renders a whole file,
which would drop someone's comments.
"""
import os
import re
import shutil

import atomic
import polarity as PO
import tomlite

# The instruction block `stub` and `extract` put at the top of a file. It
# addresses whoever still has to review the table, so signing removes it.
# Provenance lines stay: they say where the table came from.
_KEEP = ("# provenance:", "# source:")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write(path, text):
    atomic.write_text(path, text)


def set_top_key(text, key, value, after="extracted_by"):
    """Set a document-level `key = "value"`, adding it after `after`."""
    line = f"{key:<12} = {tomlite.quote(value)}"
    rx = re.compile(rf"^{re.escape(key)}\s*=.*$", re.M)
    if rx.search(text):
        return rx.sub(lambda _m: line, text, count=1)
    m = re.search(rf"^{re.escape(after)}\s*=.*$", text, re.M)
    at = m.end() if m else 0
    return text[:at] + "\n" + line + text[at:]


def set_variant_key(text, key, value):
    """Set a key inside the first [[variant]] header block. A list value
    is written as a TOML array (of arrays) of strings."""
    def toml(v):
        if isinstance(v, (list, tuple)):
            return "[" + ", ".join(toml(x) for x in v) + "]"
        return tomlite.quote(v)
    line = f"  {key:<12} = {toml(value)}"
    rx = re.compile(rf"^\s*{re.escape(key)}\s*=.*$", re.M)
    if rx.search(text):
        return rx.sub(lambda _m: line, text, count=1)
    m = re.search(r"^\s*\[\[variant\]\]\s*$", text, re.M)
    return text[:m.end()] + "\n" + line + text[m.end():]


def sign(path, by, on):
    """Record who checked this table, and drop the to-do header."""
    text = _read(path)
    lines = text.split("\n")
    head = 0
    while head < len(lines) and (lines[head].startswith("#") or not lines[head].strip()):
        head += 1
    kept = [ln for ln in lines[:head] if ln.startswith(_KEEP)]
    text = "\n".join(kept + ([""] if kept else []) + lines[head:])
    # A stub's inline hints ("# your name -- a person typed this") and its
    # quoted drawing labels are instructions too, stale once signed.
    text = re.sub(r'^(\s*(?:doc_id|extracted_by|source)\s*=\s*"[^"]*")\s*#.*$',
                  r"\1", text, flags=re.M)
    text = re.sub(r"^# LABEL ROWS.*\n(?:#.*\n)*\n?", "", text, flags=re.M)
    if re.search(r'^origin\s*=\s*"manual"', text, re.M):
        text = set_top_key(text, "extracted_by", by)
    text = set_top_key(text, "reviewed_by", by)
    text = set_top_key(text, "reviewed_on", on, after="reviewed_by")
    if re.search(r'^\s*confidence\s*=\s*"draft"', text, re.M):
        text = set_variant_key(text, "confidence", "reviewed")
    _write(path, text)


def set_pin_names(path, names):
    """Fill `N = ""` lines of a stub. names: {pin number: name}."""
    text = _read(path)
    for num, name in names.items():
        rx = re.compile(rf'^(\s*){re.escape(str(num))}\s*=\s*"[^"]*"(.*)$', re.M)
        text, n = rx.subn(lambda m: f"{m.group(1)}{num} = {tomlite.quote(name)}",
                          text, count=1)
        if not n:
            raise ValueError(f"{path}: no line for pin {num}")
    _write(path, text)


def make_numbered(path, numbers):
    """Replace the [variant.pins] table with a numbers-only claim."""
    text = _read(path)
    m = re.search(r"^\s*\[variant\.pins\]\s*$", text, re.M)
    if not m:
        raise ValueError(f"{path}: no [variant.pins] table")
    rest = text[m.end():]
    nxt = re.search(r"^\s*\[", rest, re.M)
    tail = rest[nxt.start():] if nxt else ""
    items = ", ".join(tomlite.quote(n) for n in numbers)
    body = (f"  pin_numbers  = [{items}]   # the datasheet numbers these pins "
            f"and names none of them\n")
    text = text[:m.start()].rstrip("\n") + "\n" + body + ("\n" + tail if tail else "")
    _write(path, text)


def add_pins(path, names):
    """Add pins a partial table is missing, after its last pin line."""
    text = _read(path)
    m = re.search(r"^\s*\[variant\.pins\]\s*$", text, re.M)
    if not m:
        raise ValueError(f"{path}: no [variant.pins] table")
    rest = text[m.end():]
    nxt = re.search(r"^\s*\[", rest, re.M)
    body = rest[:nxt.start()] if nxt else rest
    at = m.end() + len(body.rstrip()) if body.strip() else m.end()
    new = "".join(f"\n    {num} = {tomlite.quote(name)}   # typed by hand"
                  for num, name in names.items())
    _write(path, text[:at] + new + text[at:])


def promote(path, live_dir):
    """Move a signed draft into the checked directory, without its prefix."""
    name = re.sub(r"^(DRAFT|STUB)-", "", os.path.basename(path))
    os.makedirs(live_dir, exist_ok=True)
    dst = os.path.join(live_dir, name)
    if os.path.exists(dst):
        raise FileExistsError(dst)
    shutil.move(path, dst)
    return dst


def stub_name(mpn):
    return "STUB-" + re.sub(r"[^A-Za-z0-9._-]", "_", mpn) + ".toml"


def write_polarity_stub(att, ln, text):
    """A stub asking only what the marking on the part means, with what the
    datasheet seems to say quoted as a hint."""
    c = ln.comp
    mark, evidence, page = PO.datasheet_hint(text) if text else ("", "", 0)
    path = os.path.join(att, "reference", "draft", stub_name(ln.mpn))
    L = [f"# STUB -- {ln.mpn} is a {PO.classify(c)}: its datasheet numbers no",
         "# pins, so the claim is what the marking on the part (band, dot,",
         "# notch) means. bomcheck checks the symbol and footprint against it.",
         f"# placed as: {', '.join(ln.refs)}",
         f"# footprint: {c.footprint}",
         "",
         'doc_id       = ""',
         f"manufacturer = {tomlite.quote(c.field('Manufacturer'))}",
         'origin       = "manual"',
         'extracted_by = ""',
         "",
         "  [[variant]]",
         f"  part         = {tomlite.quote(ln.mpn)}",
         f"  package      = {tomlite.quote(c.footprint.partition(':')[2])}",
         f"  mpns         = [{tomlite.quote(ln.mpn)}]",
         '  confidence   = "manual"',
         '  marked       = ""   # cathode | anode | plus | minus | none']
    if evidence:
        L.append(f"  # datasheet page {page}: \"{evidence}\""
                 + (f" -> {mark}?" if mark else ""))
    atomic.write_text(path, "\n".join(L) + "\n")
    return path
