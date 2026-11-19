#!/usr/bin/env python3
"""Footprint geometry: which pad a 2-pad footprint marks, and a picture of it.

kicad-cli exports footprints as pictures but not their geometry, so this reads
the s-expression itself. It reads the footprint as PLACED in the .kicad_pcb:
that is what the assembly house gets, local edits included, and it needs no
library table.

THE MARKING. A polarised 2-pad footprint marks one end on silk and fab: a
cathode bar, a "+", a notch. KiCad's library puts that mark at pad 1 (the
cathode of a diode, the + of a polarised cap). The mark is found by where the
drawing's weight sits along the pad1-pad2 axis: a symmetric outline balances
out, a mark pulls the balance to its end.
"""
import math
import os
import re
import subprocess
import tempfile

import atomic

GRAPHIC_LAYERS = ("F.SilkS", "F.Fab", "B.SilkS", "B.Fab")
# Fraction of the half pad-to-pad distance the balance must move before it
# counts as a mark. Measured on KiCad's own libraries; see the tests.
MARK_MIN_SHIFT = 0.05


def _tokens(text):
    return re.findall(r'"(?:\\.|[^"\\])*"|\(|\)|[^\s()"]+', text)


def parse(text):
    """s-expression -> nested lists. Strings lose their quotes."""
    stack, cur = [], []
    for tok in _tokens(text):
        if tok == "(":
            stack.append(cur)
            cur = []
        elif tok == ")":
            done, cur = cur, stack.pop()
            cur.append(done)
        else:
            cur.append(tok[1:-1].replace('\\"', '"') if tok.startswith('"') else tok)
    return cur[0] if len(cur) == 1 else cur


def _find(node, name):
    return [c for c in node if isinstance(c, list) and c and c[0] == name]


def _one(node, name):
    hits = _find(node, name)
    return hits[0] if hits else None


def _xy(node):
    return float(node[1]), float(node[2])


def footprint_blocks(pcb_text):
    """-> {reference: raw footprint text} for every footprint in a board."""
    out = {}
    for m in re.finditer(r"\n\s*\(footprint ", pcb_text):
        start = m.start() + m.group(0).index("(")
        end = _block_end(pcb_text, start)
        block = pcb_text[start:end]
        ref = re.search(r'\(property "Reference" "([^"]*)"', block)
        if ref:
            out[ref.group(1)] = block
    return out


def _block_end(text, start):
    depth, i = 0, start
    while i < len(text):
        c = text[i]
        if c == '"':
            i += 1
            while text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unbalanced footprint block")


class Footprint:
    def __init__(self, text):
        self.text = text
        self.node = parse(text)
        self.name = self.node[1]
        self.pads = {}
        self.through_hole = any(len(p) > 2 and p[2] == "thru_hole"
                                for p in _find(self.node, "pad"))
        for p in _find(self.node, "pad"):
            num = p[1]
            if num and num not in self.pads:
                self.pads[num] = _xy(_one(p, "at"))
        self.segments = {layer: [] for layer in GRAPHIC_LAYERS}
        for kind in ("fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc"):
            for g in _find(self.node, kind):
                layer = _one(g, "layer")
                if layer and layer[1] in self.segments:
                    self.segments[layer[1]] += _segments(kind, g)

    def marked_pad(self):
        """-> (pad, detail) for a 2-pad footprint; pad is "" when unmarked.

        Silk and fab are judged separately and must agree where both mark.
        """
        if set(self.pads) != {"1", "2"}:
            return "", f"not a 2-pad footprint (pads {sorted(self.pads)})"
        # Through-hole bodies are often drawn beside the pads (a horizontal
        # LED), which moves the balance by itself. Not judged; a person looks.
        if self.through_hole:
            return "", "through-hole footprint, not judged automatically"
        (x1, y1), (x2, y2) = self.pads["1"], self.pads["2"]
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        half = math.hypot(x2 - x1, y2 - y1) / 2
        if half == 0:
            return "", "pads 1 and 2 coincide"
        ux, uy = (x2 - x1) / (2 * half), (y2 - y1) / (2 * half)
        # A "+" is unambiguous and beats the balance: a polarised cap's
        # chamfered corners pull the balance the wrong way.
        plus = [t for lay, ss in self.segments.items() for t in
                _plus_signs(ss, mx, my, ux, uy)]
        if plus:
            pad = "1" if sum(plus) < 0 else "2"
            return pad, f"a + sign marks pad {pad}"
        verdict = {}
        for layer in ("SilkS", "Fab"):
            segs = [s for lay, ss in self.segments.items() if lay.endswith(layer)
                    for s in ss]
            total = sum(length for _t, length in _weighted(segs, mx, my, ux, uy))
            if not total:
                continue
            shift = sum(t * length for t, length in
                        _weighted(segs, mx, my, ux, uy)) / total / half
            if abs(shift) >= MARK_MIN_SHIFT:
                verdict[layer] = "1" if shift < 0 else "2"
        marks = set(verdict.values())
        if not marks:
            return "", "no polarity mark on silk or fab"
        if len(marks) > 1:
            return "", f"silk marks pad {verdict['SilkS']}, fab marks pad {verdict['Fab']}"
        pad = marks.pop()
        return pad, " and ".join(sorted(verdict)) + f" mark pad {pad}"


def _segments(kind, g):
    if kind == "fp_line":
        return [(_xy(_one(g, "start")), _xy(_one(g, "end")))]
    if kind == "fp_rect":
        (ax, ay), (bx, by) = _xy(_one(g, "start")), _xy(_one(g, "end"))
        c = [(ax, ay), (bx, ay), (bx, by), (ax, by)]
        return list(zip(c, c[1:] + c[:1]))
    if kind == "fp_poly":
        pts = [_xy(p) for p in _find(_one(g, "pts"), "xy")]
        return list(zip(pts, pts[1:] + pts[:1]))
    if kind == "fp_arc":
        pts = [_xy(_one(g, k)) for k in ("start", "mid", "end")]
        return list(zip(pts, pts[1:]))
    if kind == "fp_circle":
        (cx, cy), (ex, ey) = _xy(_one(g, "center")), _xy(_one(g, "end"))
        r = math.hypot(ex - cx, ey - cy)
        pts = [(cx + r * math.cos(a * math.pi / 8), cy + r * math.sin(a * math.pi / 8))
               for a in range(16)]
        return list(zip(pts, pts[1:] + pts[:1]))
    return []


def _plus_signs(segs, mx, my, ux, uy):
    """Positions along the pad axis of "+" signs: two short strokes, one along
    the axis and one across it, crossing at their middles."""
    def mid(s):
        (ax, ay), (bx, by) = s
        return (ax + bx) / 2, (ay + by) / 2

    def length(s):
        (ax, ay), (bx, by) = s
        return math.hypot(bx - ax, by - ay)

    def along(s):
        (ax, ay), (bx, by) = s
        n = length(s) or 1
        return abs(((bx - ax) * ux + (by - ay) * uy) / n)
    short = [s for s in segs if 0 < length(s) < 2.0]
    out = []
    for a in short:
        for b in short:
            if along(a) > 0.95 and along(b) < 0.05 and \
                    abs(length(a) - length(b)) < 0.2 * length(a) and \
                    math.dist(mid(a), mid(b)) < 0.05:
                cx, cy = mid(a)
                out.append((cx - mx) * ux + (cy - my) * uy)
    return out


def _weighted(segs, mx, my, ux, uy):
    """(position along the pad axis, length) for each segment's midpoint."""
    for (ax, ay), (bx, by) in segs:
        length = math.hypot(bx - ax, by - ay)
        cx, cy = (ax + bx) / 2 - mx, (ay + by) / 2 - my
        yield cx * ux + cy * uy, length


def from_pcb(pcb_path, ref):
    blocks = footprint_blocks(open(pcb_path, encoding="utf-8").read())
    return Footprint(blocks[ref]) if ref in blocks else None


def render_svg(fp, out_dir):
    """Picture of the footprint: copper, silk and fab with pad numbers.
    -> path, or "" when kicad-cli is missing or fails."""
    name = fp.name.partition(":")[2] or fp.name
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    lib = tempfile.mkdtemp(prefix="bomcheck-fp-", suffix=".pretty")
    atomic.write_text(os.path.join(lib, safe + ".kicad_mod"), _as_library(fp.text))
    os.makedirs(out_dir, exist_ok=True)
    try:
        subprocess.run(["kicad-cli", "fp", "export", "svg", "--fp", safe,
                        "--layers", "F.Cu,F.SilkS,F.Fab,B.Cu,B.SilkS,B.Fab",
                        "--sketch-pads-on-fab-layers", "-o", out_dir, lib],
                       capture_output=True, timeout=60)
    except Exception:                                            # noqa: BLE001
        return ""
    path = os.path.join(out_dir, safe + ".svg")
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as f:
        svg = f.read()
    atomic.write_text(path, _viewable_size(_crop(svg)))
    return path


def _crop(svg, margin=0.5):
    """kicad-cli's page can be far larger than the footprint, which then sits
    small in a corner. Fit the page to what is drawn, on white: the pane
    behind it can be dark."""
    vb = re.search(r'width="[\d.]+mm" height="[\d.]+mm" viewBox="([^"]+)"', svg)
    xs, ys = [], []
    for d in re.findall(r'\bd="([^"]*)"', svg):
        for x, y in re.findall(r"(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", d):
            xs.append(float(x))
            ys.append(float(y))
    for cx, cy, r in re.findall(r'cx="([-\d.]+)" cy="([-\d.]+)" r="([\d.]+)"', svg):
        xs += [float(cx) - float(r), float(cx) + float(r)]
        ys += [float(cy) - float(r), float(cy) + float(r)]
    if not (vb and xs):
        return svg
    x0, y0 = min(xs) - margin, min(ys) - margin
    w, h = max(xs) - x0 + margin, max(ys) - y0 + margin
    svg = (svg[:vb.start()] + f'width="{w:.4f}mm" height="{h:.4f}mm" '
           f'viewBox="{x0:.4f} {y0:.4f} {w:.4f} {h:.4f}"' + svg[vb.end():])
    end = svg.find(">", svg.find("<svg")) + 1
    return (svg[:end] + f'\n<rect x="{x0:.4f}" y="{y0:.4f}" width="{w:.4f}" '
            f'height="{h:.4f}" fill="#ffffff"/>' + svg[end:])


def _viewable_size(svg, longest_px=900):
    """kicad-cli writes the real size (a 7 mm footprint), which a viewer opens
    as a speck. Scale the outer width/height; the viewBox keeps the drawing."""
    m = re.search(r'width="([\d.]+)mm" height="([\d.]+)mm"', svg)
    if not m:
        return svg
    w, h = float(m.group(1)), float(m.group(2))
    k = longest_px / max(w, h, 1e-6)
    return svg[:m.start()] + f'width="{w * k:.0f}" height="{h * k:.0f}"' + svg[m.end():]


def _as_library(block):
    """A placed footprint as a library file: drop its board position, and take
    the board rotation back out of the pad angles."""
    at = re.search(r"^(\(footprint \"[^\"]*\"[^\n]*\n(?:\s*\((?:layer|uuid)[^\n]*\n)*)"
                   r"\s*\(at ([-\d.]+) ([-\d.]+)(?: ([-\d.]+))?\)\n", block)
    rot = float(at.group(4) or 0) if at else 0.0
    if at:
        block = block[:at.start(2) - len("(at ")] .rstrip() + "\n" + block[at.end():]
        block = at.group(1) + block[len(at.group(1)):]

    def unrotate(m):
        ang = (float(m.group(3) or 0) - rot) % 360
        return f"(at {m.group(1)} {m.group(2)}" + (f" {ang:g})" if ang else ")")
    block = re.sub(r"\(at ([-\d.]+) ([-\d.]+)(?: ([-\d.]+))?\)", unrotate, block)
    return _to_top(block) if _on_back(block) else block


def _on_back(block):
    m = re.search(r'\(layer "([FB])\.Cu"\)', block[:400])
    return bool(m and m.group(1) == "B")


def _to_top(block):
    """A bottom-side part as seen from its own top, like the datasheet draws
    it. KiCad stores a flipped part with y and angles negated and B/F layers
    swapped; undo that."""
    def at(m):
        ang = -float(m.group(3) or 0) % 360
        return f"(at {m.group(1)} {_neg(m.group(2))}" + (f" {ang:g})" if ang else ")")
    block = re.sub(r"\(at ([-\d.]+) ([-\d.]+)(?: ([-\d.]+))?\)", at, block)
    block = re.sub(r"\((start|end|center|mid|xy) ([-\d.]+) ([-\d.]+)\)",
                   lambda m: f"({m.group(1)} {m.group(2)} {_neg(m.group(3))})", block)
    block = re.sub(r'"([FB])\.(Cu|SilkS|Fab|CrtYd|Paste|Mask|Adhes)"',
                   lambda m: f'"{"F" if m.group(1) == "B" else "B"}.{m.group(2)}"', block)
    return re.sub(r"\(justify((?: \w+)*?) mirror((?: \w+)*)\)",
                  lambda m: f"(justify{m.group(1)}{m.group(2)})" if (m.group(1) + m.group(2)) else "",
                  block)


def _neg(v):
    return v[1:] if v.startswith("-") else ("-" + v if float(v) else v)


def signed_path(att):
    return os.path.join(att, "footprints.toml")


def load_signed(att):
    """{footprint name: marked pad} a person confirmed where the geometry
    check could not judge."""
    import tomlite
    path = signed_path(att)
    if not os.path.isfile(path):
        return {}
    return {str(d.get("name")): str(d.get("marked_pad", ""))
            for d in tomlite.load(path).get("footprint") or [] if d.get("by")}


def add_signed(att, name, pad, by, on):
    import tomlite
    path = signed_path(att)
    text = (open(path, encoding="utf-8").read() if os.path.isfile(path) else
            "# Which pad a footprint's polarity marking points at, confirmed by a\n"
            "# person where bomcheck could not tell from the silk and fab.\n")
    entry = ["", "[[footprint]]", f"name       = {tomlite.quote(name)}",
             f"marked_pad = {tomlite.quote(pad)}   # \"\" = no marking",
             f"by         = {tomlite.quote(by)}", f"on         = {tomlite.quote(on)}"]
    atomic.write_text(path, text.rstrip("\n") + "\n" + "\n".join(entry) + "\n")


def board_marks(project, att):
    """{reference: (footprint name, marked pad, how)} for the board's 2-pad
    footprints, the geometry check first and a person's confirmation second."""
    import glob
    pcbs = glob.glob(os.path.join(project, "*.kicad_pcb"))
    if len(pcbs) != 1:
        return {}
    signed = load_signed(att)
    out = {}
    for ref, block in footprint_blocks(open(pcbs[0], encoding="utf-8").read()).items():
        fp = Footprint(block)
        if set(fp.pads) != {"1", "2"}:
            continue
        pad, why = fp.marked_pad()
        if not pad and fp.name in signed:
            pad, why = signed[fp.name], "confirmed by a person"
            if not pad:
                why = "confirmed unmarked by a person"
        out[ref] = (fp.name, pad, why, fp)
    return out
