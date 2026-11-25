"""Interface tables: what an interface spec says each pin of a symbol is.

A connector that implements an interface (SL23) has a datasheet that only
numbers its contacts. The pin names on its symbol come from the interface
spec, so the spec is their reference: a signed table, pin number = signal
name, keyed to the symbols that follow it. A symbol names its interface in
the field `Pinout_Spec` (todo.FIELD).

The spec may be a URL or a file, often markdown. A pin table in a markdown
spec can be read to pre-fill a draft, but only as a hint next to each pin:
the person still confirms every name against the spec. The names never come
from the symbol, or the table would agree with the thing it checks.
"""
import os
import re

import atomic
import tomlite

_PIN_HEAD = re.compile(r"^(pin|pins|pin ?(no\.?|nr\.?|number|#)|#|contact|no\.?)$", re.I)
_NAME_HEAD = re.compile(r"^(signal|signal name|name|net|pin name)$", re.I)


def _cells(line):
    s = line.strip()
    if not (s.startswith("|") and s.endswith("|")) or len(s) < 2:
        return None
    return [c.strip() for c in s[1:-1].split("|")]


def _clean(cell):
    """`IO_1` or **IO_1** -> IO_1."""
    return re.sub(r"[`*]", "", cell).strip()


def spec_tables(text):
    """-> [(heading, {pin: name})] for each markdown table in a spec that has
    a pin column and a signal column. A pin cell may list several pins
    ("2, 4"); a cell that is not plain numbers is left out."""
    out = []
    lines = text.splitlines()
    heading = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
        head = _cells(line)
        sep = _cells(lines[i + 1]) if i + 1 < len(lines) else None
        if head and sep and all(re.fullmatch(r":?-{3,}:?", c) for c in sep if c):
            pcol = next((k for k, c in enumerate(head) if _PIN_HEAD.match(_clean(c))), None)
            ncol = next((k for k, c in enumerate(head) if _NAME_HEAD.match(_clean(c))), None)
            i += 2
            pins = {}
            while i < len(lines) and _cells(lines[i]) is not None:
                row = _cells(lines[i])
                i += 1
                if pcol is None or ncol is None or max(pcol, ncol) >= len(row):
                    continue
                nums = [n for n in re.split(r"[,\s]+", _clean(row[pcol])) if n]
                name = _clean(row[ncol])
                if not name or not nums or not all(n.isdigit() for n in nums):
                    continue
                for n in nums:
                    pins[n] = name
            if pins:
                out.append((heading, pins))
            continue
        i += 1
    return out


def is_url(spec):
    return spec.lower().startswith(("http://", "https://"))


def resolve_spec(spec, base_dir):
    """-> a URL, or an absolute path to an existing file, or "". A relative
    path is relative to the table's own folder."""
    spec = (spec or "").strip()
    if not spec or is_url(spec):
        return spec
    if spec.startswith("file://"):
        spec = spec[7:]
    p = os.path.expanduser(spec)
    p = p if os.path.isabs(p) else os.path.join(base_dir, p)
    return os.path.normpath(p) if os.path.isfile(p) else ""


def write_stub(path, name, lib_id, nums, spec, hints=None, source=""):
    """A blank interface table for one symbol. `hints` {pin: name} read from
    the spec go in a comment after each blank pin, never in the value: only
    a person's answer fills a pin."""
    hints = hints or {}
    q = tomlite.quote
    L = [f"# DRAFT interface table -- the names as the {name} spec gives them.",
         "# Type each pin from the spec, never from the symbol.",
         *([f"# provenance: hints read from {source}; each one still needs a "
            f"person to check it"] if hints else []),
         "",
         f"interface    = {q(name)}",
         f"spec         = {q(spec)}",
         f'origin       = {q("spec-read" if hints else "manual")}',
         'extracted_by = ' + q("bomcheck (hints only)" if hints else ""),
         "",
         "  [[variant]]",
         f"  part         = {q(name + ' ' + (lib_id.partition(':')[2] or lib_id))}",
         f"  symbols      = [{q(lib_id)}]",
         '  confidence   = "draft"',
         f"  pin_count    = {len(nums)}",
         "",
         "    [variant.pins]"]
    for n in nums:
        hint = hints.get(n, "")
        L.append(f'    {n} = ""' + (f"   # spec: {hint}" if hint else ""))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic.write_text(path, "\n".join(L) + "\n")
    return path


def hints_of(path):
    """{pin: name} from the `# spec: X` comments of a draft's blank pins."""
    out = {}
    for m in re.finditer(r'^\s*([A-Za-z0-9]+)\s*=\s*""\s*#\s*spec:\s*(.+?)\s*$',
                         open(path, encoding="utf-8").read(), re.M):
        out[m.group(1)] = m.group(2)
    return out


def parse_shared(answer, pins):
    """`GND = 2-50/2, 1` -> ("GND", ["2", "4", .., "50", "1"], [pins the
    part does not have]). -> (name, [], []) when the list is not understood."""
    name, eq, spec = answer.partition("=")
    name = name.strip()
    if not eq or not name:
        return name, [], []
    out = []
    for tok in re.split(r"[,\s]+", spec.strip()):
        m = re.fullmatch(r"(\d+)(?:-(\d+)(?:/(\d+))?)?", tok)
        if not tok:
            continue
        if not m:
            return name, [], []
        a = int(m.group(1))
        b = int(m.group(2) or a)
        step = int(m.group(3) or 1)
        if b < a or step < 1:
            return name, [], []
        out += [str(n) for n in range(a, b + 1, step)]
    out = list(dict.fromkeys(out))
    return name, [n for n in out if n in pins], [n for n in out if n not in pins]
