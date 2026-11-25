"""What a symbol's drawing says beyond its pin names.

Read from the symbol copies a schematic keeps in (lib_symbols):
  units   which pins belong to which unit (Q1A, Q1B of a dual FET)
  joined  which pins the drawing joins with lines and arcs (a choke's coils)
  dots    which pin each small filled circle sits next to (winding start)

Only lines and arcs join pins. A rectangle does not: a connector's body
touches every pin and joins none of them. When nothing is joined the drawing
could not be read this way, and a person looks at a picture instead.
"""
import glob
import math
import os
import re
import subprocess
import tempfile

import atomic
import footprint as FP

TOL = 0.02   # mm; KiCad snaps to a 0.0254 mm grid at the finest


class Symbol:
    __slots__ = ("lib_id", "units", "joined", "dots", "text")

    def __init__(self, lib_id, text):
        self.lib_id, self.text = lib_id, text
        self.units, self.joined, self.dots = _read(FP.parse(text))

    def unit_of(self):
        """{pin: unit letter}, only for a symbol with two or more units."""
        if len(self.units) < 2:
            return {}
        return {p: chr(ord("A") + u - 1) for u, pins in self.units.items()
                for p in pins}


def load(project):
    """{lib_id: Symbol} for every symbol the project's schematics use."""
    out = {}
    import schematic as SCH
    for sch in SCH.sheets(project):
        text = open(sch, encoding="utf-8").read()
        lib = _block_at(text, text.find("(lib_symbols"))
        if not lib:
            continue
        pos = len("(lib_symbols")
        while True:
            m = re.compile(r'\(symbol "([^"]+)"').search(lib, pos)
            if not m:
                break
            block = _block_at(lib, m.start())
            out.setdefault(m.group(1), Symbol(m.group(1), block))
            pos = m.start() + len(block)
    return out


def _block_at(text, start):
    if start < 0:
        return ""
    depth, i, n = 0, start, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    return ""


def _read(tree):
    """Each unit is traced on its own: units are drawn at the same
    coordinates, so tracing them together joins pins of different units."""
    units, per = {}, {}   # per[unit] = (pin ends, segments, dot centres)
    for sub in FP._find(tree, "symbol"):
        m = re.search(r"_(\d+)_(\d+)$", sub[1])
        unit = int(m.group(1)) if m else 0
        ends, segs, circles = per.setdefault(unit, ({}, [], []))
        for pin in FP._find(sub, "pin"):
            at, length, num = FP._one(pin, "at"), FP._one(pin, "length"), FP._one(pin, "number")
            if not (at and num):
                continue
            x, y = FP._xy(at)
            a = math.radians(float(at[3]) if len(at) > 3 else 0)
            ln = float(length[1]) if length else 0
            ends[num[1]] = (x + ln * math.cos(a), y + ln * math.sin(a))
            if unit:
                units.setdefault(unit, set()).add(num[1])
        for poly in FP._find(sub, "polyline"):
            pts = [FP._xy(p) for p in FP._find(FP._one(poly, "pts") or [], "xy")]
            segs += list(zip(pts, pts[1:]))
        for arc in FP._find(sub, "arc"):
            s, e = FP._one(arc, "start"), FP._one(arc, "end")
            if s and e:
                segs.append((FP._xy(s), FP._xy(e)))
        for c in FP._find(sub, "circle"):
            centre, r = FP._one(c, "center"), FP._one(c, "radius")
            if centre and r and float(r[1]) < 0.5:
                circles.append(FP._xy(centre))
    common = per.pop(0, ({}, [], []))
    joined, dots = [], set()
    for unit in sorted(per) or [0]:
        ends, segs, circles = per.get(unit, ({}, [], []))
        j, d = _trace({**common[0], **ends}, common[1] + segs, common[2] + circles)
        joined += j
        dots |= d
    return units, sorted(joined, key=lambda g: _pinsort(g[0])), \
        sorted(dots, key=_pinsort)


def _trace(ends, segs, circles):
    """-> ([[pins joined by lines and arcs]], {pins next to a dot})"""
    parent = {}

    def key(p):
        return (round(p[0] / TOL), round(p[1] / TOL))

    def root(k):
        parent.setdefault(k, k)
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for a, b in segs:
        parent[root(key(a))] = root(key(b))
    groups = {}
    for num, p in ends.items():
        if key(p) in parent:
            groups.setdefault(root(key(p)), set()).add(num)
    joined = [sorted(g, key=_pinsort) for g in groups.values() if len(g) > 1]
    in_joined = [n for g in joined for n in g]
    dots = {min(in_joined, key=lambda n: math.dist(c, ends[n]))
            for c in circles} if in_joined else set()
    return joined, dots


def _pinsort(n):
    return (0, int(n), "") if n.isdigit() else (1, 0, n)


def partition(groups):
    """Comparable form of [[pins], ...]: a set of frozensets, no singletons."""
    return {frozenset(map(str, g)) for g in groups if len(g) > 1}


def render_svg(sym, out_dir):
    """Picture of the symbol with its pin numbers. -> path, or ""."""
    name = sym.lib_id.partition(":")[2] or sym.lib_id
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    text = sym.text.replace(f'(symbol "{sym.lib_id}"', f'(symbol "{name}"', 1)
    lib = os.path.join(tempfile.mkdtemp(prefix="bomcheck-sym-"), "s.kicad_sym")
    atomic.write_text(lib, '(kicad_symbol_lib (version 20231120) (generator "bomcheck")\n'
                      + text + "\n)\n")
    os.makedirs(out_dir, exist_ok=True)
    try:
        subprocess.run(["kicad-cli", "sym", "export", "svg", "--symbol", name,
                        "-o", out_dir, lib], capture_output=True, timeout=60)
    except Exception:                                            # noqa: BLE001
        return ""
    hits = sorted(glob.glob(os.path.join(out_dir, f"{name}*.svg")) +
                  glob.glob(os.path.join(out_dir, f"{safe}*.svg")))
    if not hits:
        return ""
    path = hits[0]
    svg = open(path, encoding="utf-8").read()
    atomic.write_text(path, FP._viewable_size(svg))
    return path
