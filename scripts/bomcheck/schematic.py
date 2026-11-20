#!/usr/bin/env python3
"""Read and WRITE symbol properties in a .kicad_sch, surgically.

WHY WRITE AT ALL. The goal is for the schematic to be the source of truth for
Manufacturer, MPN and datasheet URL. Forty-one suggested fields is an afternoon
of clicking in the KiCad GUI, and an afternoon of clicking is how fields stay
empty. So the accepted suggestions are applied for you.

HOW IT STAYS SAFE. Every edit is a BYTE SPLICE into the original text: the file
is not re-serialised, so nothing outside the property being changed can move,
gain whitespace, or lose a field this tool does not understand. Three further
rules, in `apply_edits`:

  * the file must be clean in git, so any change is visible as a diff and
    revertible with one command;
  * the edited text is re-parsed and the new value read back before it is
    written, so a bad splice fails instead of saving;
  * only the INSTANCE is touched. A .kicad_sch embeds a copy of each library
    symbol under (lib_symbols ...), carrying the same property names -- editing
    that copy would change nothing on the board and would silently diverge from
    the library. Instances are the top-level (symbol ...) blocks only.
"""
import os
import re
import subprocess

import atomic

_TOKEN = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')


def _blocks(text, name, top_level_only=True):
    """-> [(start, end)] for every (name ...) block, by a depth-aware scan.

    A regex window cannot do this: symbol blocks nest, and a fixed-size window
    runs into the NEXT symbol and reads its Reference. That mistake made U16
    look like a Device:C_Small.
    """
    out, depth, start, want = [], 0, None, None
    for m in _TOKEN.finditer(text):
        tok = m.group(0)
        if tok == "(":
            depth += 1
            nxt = _TOKEN.search(text, m.end())
            if nxt and nxt.group(0) == name and start is None:
                if not top_level_only or depth == 2:
                    start, want = m.start(), depth
        elif tok == ")":
            if start is not None and depth == want:
                out.append((start, m.end()))
                start, want = None, None
            depth -= 1
    return out


def _prop_span(block, name):
    """-> (value, value_start, value_end) for a property inside one block."""
    for s, e in _blocks(block, "property", top_level_only=False):
        m = re.match(r'\(\s*property\s+"((?:[^"\\]|\\.)*)"\s+"((?:[^"\\]|\\.)*)"',
                     block[s:e])
        if m and m.group(1) == name:
            vs = s + m.start(2)
            return m.group(2), vs, vs + len(m.group(2))
    return None, None, None


_INST_REF = re.compile(r'\(\s*reference\s+"((?:[^"\\]|\\.)*)"')


class Instance:
    """One placed symbol -- which may carry SEVERAL designators.

    A sheet used more than once reuses one symbol block for every copy: the
    (property "Reference" ...) holds only the default, and the real designators
    live in the (instances ...) block, one per sheet path. On this board a
    single Conn_01x03 in pt100_input.kicad_sch is J5, J6, J7 AND J8.

    Two consequences, both load-bearing:
      * looking a part up by its designator must search these, or J7 is simply
        "not found" while plainly existing on the board;
      * writing a property here changes it for EVERY designator the block
        serves. That is usually right (they are the same part) but it must never
        be silent, so `refs` is reported whenever an edit touches more than one.
    """

    __slots__ = ("path", "ref", "refs", "start", "end", "text")

    def __init__(self, path, ref, start, end, text, refs=None):
        self.path, self.ref = path, ref
        self.start, self.end, self.text = start, end, text
        self.refs = refs or [ref]

    def prop(self, name):
        v = _prop_span(self.text, name)[0]
        return None if v is None else re.sub(r"\\(.)", r"\1", v)

    def __repr__(self):
        extra = f" (+{len(self.refs) - 1} more)" if len(self.refs) > 1 else ""
        return f"Instance({os.path.basename(self.path)}:{self.ref}{extra})"


_INST_PROJECT = re.compile(r'\(\s*project\s+"((?:[^"\\]|\\.)*)"')


def _refs_of(block, default, project=""):
    """Every designator this symbol block serves in THIS project. A symbol
    copied from another project keeps that project's entry in (instances), with
    its old designator: on one board U7's block also said U4, and an edit for U4
    landed on U7. Only when no entry names the project are all of them used."""
    per = {}
    for s, e in _blocks(block, "instances", top_level_only=False):
        inst = block[s:e]
        for ps, pe in _blocks(inst, "project", top_level_only=False):
            m = _INST_PROJECT.match(inst, ps)
            name = m.group(1) if m else ""
            per.setdefault(name, []).extend(
                r.group(1) for r in _INST_REF.finditer(inst[ps:pe]))
        if not per:   # older files: references directly under (instances)
            per[""] = [r.group(1) for r in _INST_REF.finditer(inst)]
    refs = per.get(project) if project in per else [r for v in per.values() for r in v]
    return sorted(set(refs)) or [default]


def project_name(path):
    """The KiCad project a sheet belongs to: the one .kicad_pro beside it."""
    import glob
    pros = glob.glob(os.path.join(os.path.dirname(os.path.abspath(path)), "*.kicad_pro"))
    return os.path.basename(pros[0])[:-len(".kicad_pro")] if len(pros) == 1 else ""


def instances(path):
    """-> [Instance] for every placed symbol in one schematic file."""
    text = open(path, encoding="utf-8").read()
    project = project_name(path)
    out = []
    for s, e in _blocks(text, "symbol"):
        block = text[s:e]
        ref, _, _ = _prop_span(block, "Reference")
        if ref:
            out.append(Instance(path, ref, s, e, block, _refs_of(block, ref, project)))
    return out


def sheets(project_dir):
    """-> the .kicad_sch files the design uses: the root sheet and the
    sheets it pulls in. A board folder can hold old or variant schematics
    with the same designators; those are not the design."""
    import glob
    pros = sorted(glob.glob(os.path.join(project_dir, "*.kicad_pro")))
    root = pros and os.path.splitext(pros[0])[0] + ".kicad_sch"
    if len(pros) != 1 or not os.path.isfile(root):
        return sorted(glob.glob(os.path.join(project_dir, "*.kicad_sch")))
    out, todo = [], [root]
    while todo:
        f = todo.pop(0)
        if f in out or not os.path.isfile(f):
            continue
        out.append(f)
        text = open(f, encoding="utf-8").read()
        todo += [os.path.join(os.path.dirname(f), m) for m in
                 re.findall(r'\(property "Sheet ?[Ff]ile" "([^"]+)"', text)]
    return out


def find(project_dir, ref):
    """-> Instance serving this designator, searching the design's sheets."""
    for f in sheets(project_dir):
        for inst in instances(f):
            if ref in inst.refs:
                return inst
    return None


def _indent_of(block, pos):
    line_start = block.rfind("\n", 0, pos) + 1
    return block[line_start:pos]


def set_property(text, inst, name, value):
    """-> new file text with one property set, or added if absent.

    Splices bytes; never re-serialises. A property that does not exist is
    inserted after the last existing one, copying its indentation and hidden
    style, so KiCad sees a normal field and a diff shows only the addition.
    """
    block = text[inst.start:inst.end]
    cur, vs, ve = _prop_span(block, name)
    if cur is not None:
        esc = value.replace("\\", "\\\\").replace('"', '\\"')
        return text[:inst.start + vs] + esc + text[inst.start + ve:]

    spans = _blocks(block, "property", top_level_only=False)
    if not spans:
        raise ValueError(f"{inst.ref}: no properties to anchor a new one to")
    last_end = spans[-1][1]
    indent = _indent_of(block, spans[-1][0])
    esc = value.replace("\\", "\\\\").replace('"', '\\"')
    new = (f'\n{indent}(property "{name}" "{esc}"\n'
           f'{indent}\t(at 0 0 0)\n'
           f'{indent}\t(show_name no)\n'
           f'{indent}\t(do_not_autoplace no)\n'
           f'{indent}\t(hide yes)\n'
           f'{indent}\t(effects\n'
           f'{indent}\t\t(font\n'
           f'{indent}\t\t\t(size 1.27 1.27)\n'
           f'{indent}\t\t)\n'
           f'{indent}\t)\n'
           f'{indent})')
    at = inst.start + last_end
    return text[:at] + new + text[at:]


def git_dirty(path):
    """True if the file has uncommitted changes -- refuse to edit over them."""
    d = os.path.dirname(os.path.abspath(path))
    try:
        r = subprocess.run(["git", "-C", d, "status", "--porcelain", "--", path],
                           capture_output=True, timeout=20)
        return bool(r.stdout.strip())
    except Exception:                                            # noqa: BLE001
        return False


def apply_edits(edits, dry_run=False, allow_dirty=False):
    """edits: [(ref, field, value)] -> (applied, [problem]).

    Grouped per file so one save covers a sheet, and every file is verified by
    re-parsing before it is written.
    """
    problems, applied = [], []
    by_file = {}
    for ref, field, value, inst in edits:
        by_file.setdefault(inst.path, []).append((ref, field, value))

    for path, items in sorted(by_file.items()):
        if not allow_dirty and git_dirty(path):
            problems.append(f"{os.path.basename(path)} has uncommitted changes "
                            f"-- commit or stash first, so the edit is a clean "
                            f"diff ({len(items)} edit(s) skipped)")
            continue
        text = open(path, encoding="utf-8").read()
        project = project_name(path)
        for ref, field, value in items:
            inst = next((i for i in instances(path) if ref in i.refs), None)
            if inst is None:
                problems.append(f"{ref}: no longer in {os.path.basename(path)}")
                continue
            # Re-locate on the CURRENT text: an earlier splice moved offsets.
            fresh = next(i for i in _instances_of(text, project) if ref in i.refs)
            try:
                text = set_property(text, fresh, field, value)
            except ValueError as e:
                problems.append(str(e))
                continue
            applied.append((ref, field, value))
            if len(inst.refs) > 1:
                others = [r for r in inst.refs if r != ref]
                problems.append(f"{ref}.{field}: this symbol also serves "
                                f"{', '.join(others)} (a sheet used more than "
                                f"once) -- the value applies to all of them")

        # Read back before writing: a bad splice must fail, not save.
        check = [i for i in _instances_of(text, project)]
        for ref, field, value in items:
            got = next((i.prop(field) for i in check if ref in i.refs), None)
            if got != value:
                problems.append(f"{ref}.{field}: verification failed "
                                f"(got {got!r}) -- {os.path.basename(path)} "
                                f"NOT written")
                break
        else:
            if not dry_run:
                atomic.write_text(path, text)
    return applied, problems


def _instances_of(text, project=""):
    out = []
    for s, e in _blocks(text, "symbol"):
        block = text[s:e]
        ref, _, _ = _prop_span(block, "Reference")
        if ref:
            out.append(Instance("", ref, s, e, block, _refs_of(block, ref, project)))
    return out
