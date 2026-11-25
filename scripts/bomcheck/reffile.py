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
import pintable as P
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


# ---- alias blocks ------------------------------------------------------------
# [[variant.alias_for]] blocks: aliases accepted for the symbols they list.
_VARIANT = re.compile(r"^[ \t]*\[\[variant\]\][ \t]*(#.*)?$", re.M)
_BLOCK = re.compile(r"^[ \t]*\[\[variant\.alias_for\]\][ \t]*(#.*)?$", re.M)
_PINS = re.compile(r"^[ \t]*\[variant\.alias_for\.pins\][ \t]*(#.*)?$", re.M)
_HEADER = re.compile(r"^[ \t]*\[\[?[A-Za-z_]", re.M)
_SYMBOLS = re.compile(r"^[ \t]*symbols\s*=\s*\[[^\]]*\][^\n]*$", re.M)


def _variant_span(text, mpn):
    """-> (start, end) of the [[variant]] section for this MPN. An
    interface table has no MPNs: its first variant."""
    heads = [m.start() for m in _VARIANT.finditer(text)]
    if not heads:
        raise ValueError("no [[variant]] in the table")
    vs = tomlite.loads(text).get("variant") or []
    k = next((i for i, v in enumerate(vs) if mpn in (v.get("mpns") or [])), 0)
    return heads[k], heads[k + 1] if k + 1 < len(heads) else len(text)


def _alias_blocks(text, start, end):
    """-> [(start, end, symbols)] of the alias blocks in one variant. A
    block runs to the next header that is not its own pins table."""
    out = []
    for m in _BLOCK.finditer(text, start, end):
        stop, pos = end, m.end()
        for h in _HEADER.finditer(text, pos, end):
            if not _PINS.match(text, h.start()):
                stop = h.start()
                break
        sym = _SYMBOLS.search(text, m.end(), stop)
        syms = tomlite.loads(sym.group(0).strip())["symbols"] if sym else []
        out.append((m.start(), stop, [str(x) for x in syms]))
    return out


def _symbols_line(symbols):
    return "    symbols = [" + ", ".join(tomlite.quote(s) for s in symbols) + "]"


def _block_for(text, mpn, symbol):
    start, end = _variant_span(text, mpn)
    return end, next((b for b in _alias_blocks(text, start, end)
                      if P.symbol_matches(b[2], symbol)), None)


def add_alias_block(path, mpn, symbol):
    """An alias block for `symbol`, empty, at the end of the variant. Does
    nothing when there is one."""
    text = _read(path)
    end, block = _block_for(text, mpn, symbol)
    if block is not None:
        return
    new = ("\n\n    [[variant.alias_for]]   # accepted for these symbols only\n"
           + _symbols_line([symbol]) + "\n\n    [variant.alias_for.pins]\n")
    tail = text[end:]
    _write(path, text[:end].rstrip("\n") + new + ("\n" + tail if tail.strip() else ""))


def add_alias_entry(path, mpn, symbol, entry, key):
    """Put one alias line into the block for `symbol`, made if needed.
    `entry` is the whole line."""
    add_alias_block(path, mpn, symbol)
    text = _read(path)
    _end, block = _block_for(text, mpn, symbol)
    b0, b1, _syms = block
    region = text[b0:b1]
    rx = re.compile(rf"^[ \t]*{re.escape(key)}\s*=.*$", re.M)
    m = _PINS.search(region)
    if m is None:
        region = region.rstrip("\n") + "\n\n    [variant.alias_for.pins]\n"
        m = _PINS.search(region)
    body = region[m.end():]
    if rx.search(body):
        body = rx.sub(lambda _m: entry, body, count=1)
    else:
        body = body.rstrip("\n") + "\n" + entry + "\n"
    region = region[:m.end()] + body
    tail = text[b1:]
    _write(path, text[:b0] + region + ("\n" + tail if tail.strip() else ""))


def add_block_symbol(path, mpn, symbols, symbol):
    """Add `symbol` to the alias block that lists exactly `symbols`."""
    text = _read(path)
    start, end = _variant_span(text, mpn)
    for b0, b1, syms in _alias_blocks(text, start, end):
        if syms == list(symbols):
            m = _SYMBOLS.search(text, b0, b1)
            line = _symbols_line(syms + [symbol])
            _write(path, text[:m.start()] + line + text[m.end():])
            return
    raise ValueError(f"{path}: no alias block for {', '.join(symbols)}")


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
