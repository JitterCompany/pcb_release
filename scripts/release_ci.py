#!/usr/bin/env python3
"""Config-driven PCB release  (generic — destined for common/kicad_pcb_release; no project specifics).

Inverts the old interactive release_pcb.py: `release.toml` declares INTENT +
requirements + stackup strictness; CI GENERATES the matching manufacturing
outputs, then EXTRACTS facts from the structured exports (never regexes the
board file, except the small via-tenting block that no export carries), RESOLVES
them against the config, and emits:

  * release_spec.toml            -- committed, drift-checked (like pinmap.toml).
                                    Each value carries a terse provenance comment.
  * production/README-manufacturing.md  -- the fab-spec checklist.

Modes:
  --build   generate outputs into production/, emit spec + README, run checks.
  --drift   regenerate spec to a temp dir and diff vs the committed one (CI).
  --check   run the release checks only.

Fab-spec sources: gerber job file (.gbrjob JSON) for size/thickness/layers/
finish/impedance/material-stackup/soldermask-colour; pick&place CSVs for the
SMD/THT x side counts; the .kicad_pcb (setup) block for via tenting; and (reusing
Kicad_bom_sync's netlist_reader) the BOM ref set for the pick&place >= BOM check.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kicad_netlist as K

# ----------------------------- tiny TOML reader (no deps; py3.8+) -----------------------------
def _val(v):
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        return [_val(x) for x in inner.split(",")] if inner else []
    if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
        return v[1:-1]
    if v in ("true", "false"):
        return v == "true"
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return v

def load_toml(path):
    """Parse the small subset used by release.toml: [section], key = string|bool|int|[strings]."""
    root, cur = {}, None
    for raw in open(path):
        line = raw.split("#", 1)[0].strip()          # (no '#' inside our string values)
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = root.setdefault(line[1:-1].strip(), {})
        elif "=" in line and cur is not None:
            k, v = line.split("=", 1)
            cur[k.strip()] = _val(v)
    return root

# ----------------------------- kicad-cli helpers -----------------------------
def cli(*args):
    subprocess.run(["kicad-cli", *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def generate_pos(pcb, outdir):
    """Just the pick&place CSVs (all placed + SMD-only), DNP excluded -- light,
    no gerbers, for the early pos>=BOM check."""
    os.makedirs(outdir, exist_ok=True)
    cli("pcb", "export", "pos", "--format", "csv", "--units", "mm", "--side", "both",
        "--exclude-dnp", "-o", outdir + "/all-pos.csv", pcb)
    cli("pcb", "export", "pos", "--format", "csv", "--units", "mm", "--side", "both",
        "--exclude-dnp", "--smd-only", "-o", outdir + "/smd-pos.csv", pcb)

def generate(pcb, sch, outdir):
    """All structured exports the spec/checks read from."""
    os.makedirs(outdir, exist_ok=True)
    cli("pcb", "export", "gerbers", "-o", outdir + "/", pcb)            # -> *.gbrjob + layers
    cli("pcb", "export", "drill", "--excellon-separate-th", "-o", outdir + "/", pcb)
    generate_pos(pcb, outdir)

# ----------------------------- extractors (read structured exports) -----------------------------
def extract_gbrjob(outdir):
    jobs = glob.glob(os.path.join(outdir, "*.gbrjob"))
    if not jobs:
        sys.exit("release: no .gbrjob produced -- gerber export failed?")
    d = json.load(open(jobs[0]))
    gs = d.get("GeneralSpecs", {})
    ms = d.get("MaterialStackup", [])
    copper = [m for m in ms if m.get("Type") == "Copper"]
    diel = [m for m in ms if m.get("Type") == "Dielectric"]
    smask = next((m for m in ms if m.get("Type") == "SolderMask"), {})
    fin = (gs.get("Finish") or "").strip()
    if fin.lower() == "none":                        # KiCad writes literal "None" when unset
        fin = ""
    return {
        "size": gs.get("Size", {}),
        "thickness": gs.get("BoardThickness"),
        "layers": gs.get("LayerNumber"),
        "finish": fin,
        "impedance": bool(gs.get("ImpedanceControlled")),
        "soldermask_color": smask.get("Color", ""),
        "copper": copper,
        "dielectric": diel,
    }

def extract_edge_width(outdir):
    """Edge.Cuts cut-line width, read from the generated gerber aperture. The fab
    routes on the outline CENTERLINE, but .gbrjob Size is the bounding box that
    INCLUDES this stroke -- so the true board size is gbrjob Size minus this."""
    # kicad-cli names Edge.Cuts *.gm1 (Protel ext) by default, *.gbr with Protel
    # extensions off -- match any extension.
    for f in glob.glob(os.path.join(outdir, "*Edge_Cuts*")):
        m = re.search(r"%ADD\d+C,([\d.]+)", open(f).read())
        if m:
            return float(m.group(1))
    return 0.0

def extract_tenting(pcb):
    """The one .kicad_pcb parse: the (setup ...) via-protection block (no export carries it)."""
    t = open(pcb).read()
    setup = t[t.find("(setup"):t.find("(setup") + 4000]
    def pair(name):
        m = re.search(r"\(%s\s*\(front (\w+)\)\s*\(back (\w+)\)" % name, setup)
        return (m.group(1) == "yes", m.group(2) == "yes") if m else (False, False)
    def flag(name):
        m = re.search(r"\(%s (\w+)\)" % name, setup)
        return bool(m and m.group(1) == "yes")
    tent_f, tent_b = pair("tenting")
    cov_f, cov_b = pair("covering")
    plug_f, plug_b = pair("plugging")
    return {"tenting": (tent_f, tent_b), "covering": (cov_f, cov_b),
            "plugging": (plug_f, plug_b), "capping": flag("capping"), "filling": flag("filling")}

def _pos_refs(path):
    """-> {ref: side} from a kicad-cli pos CSV."""
    out = {}
    if not os.path.isfile(path):
        return out
    rows = [r for r in open(path).read().splitlines() if r.strip()]
    if not rows:
        return out
    hdr = [c.strip().strip('"').lower() for c in rows[0].split(",")]
    ri = hdr.index("ref") if "ref" in hdr else 0
    si = hdr.index("side") if "side" in hdr else len(hdr) - 1
    for r in rows[1:]:
        c = [x.strip().strip('"') for x in r.split(",")]
        if len(c) > max(ri, si):
            out[c[ri]] = c[si].lower()
    return out

def extract_pos(outdir):
    """-> (all_placed {ref: 'top'|'bottom'}, smd_ref_set)."""
    allp = {r: ("top" if s in ("top", "front") else "bottom")
            for r, s in _pos_refs(os.path.join(outdir, "all-pos.csv")).items()}
    smd = set(_pos_refs(os.path.join(outdir, "smd-pos.csv")))
    return allp, smd

def count_assembly(pos_all, pos_smd, bom):
    """SMD/THT x side counts over BOM parts that are actually placed -- excludes
    test points / fiducials / logos that are placed but not purchased BOM parts."""
    counts = {"smd": {"top": 0, "bottom": 0}, "tht": {"top": 0, "bottom": 0}}
    for ref, side in pos_all.items():
        if ref in bom:
            counts["smd" if ref in pos_smd else "tht"][side] += 1
    return counts

def bom_refs(sch):
    """Refs that SHOULD be placed: every component minus native-flagged
    (dnp / exclude_from_bom / exclude_from_pos_files) and legacy-DNP-marked.
    Detection is a proven superset of Kicad_bom_sync's -- see internal/kicad_netlist.py."""
    fd, xml = tempfile.mkstemp(suffix=".xml"); os.close(fd)
    cli("sch", "export", "netlist", "--format", "kicadxml", "-o", xml, sch)
    comps = K.parse(xml)
    os.unlink(xml)
    return {c.ref for c in comps if not K.not_placed(c)}

# ----------------------------- resolve -----------------------------
SIDES = ("top", "bottom")
def sides_with_parts(counts):
    return [s for s in SIDES if counts["smd"][s] or counts["tht"][s]]

def resolve(cfg, gbr, tent, counts):
    warn = []
    asm_cfg = cfg.get("assembly", {}).get("sides", "auto")
    if asm_cfg == "none":
        asm = []
    elif asm_cfg in ("auto", ""):
        asm = sides_with_parts(counts)
    elif asm_cfg == "both":
        asm = list(SIDES)
    else:
        asm = [{"btm": "bottom"}.get(asm_cfg, asm_cfg)]
    # cross-check: a populated side excluded from assembly
    for s in sides_with_parts(counts):
        if s not in asm:
            warn.append(f"side '{s}' has placed parts but assembly.sides='{asm_cfg}' excludes it")

    force = cfg.get("stencil", {}).get("force", "")
    if force == "none":
        stencil = []
    elif force in ("", "auto"):
        stencil = [s for s in asm if counts["smd"][s]]     # SMD sides being assembled
    elif force == "both":
        stencil = list(SIDES)
    else:
        stencil = [{"btm": "bottom"}.get(force, force)]
    for s in asm:                                          # SMD side assembled without a stencil
        if counts["smd"][s] and s not in stencil:
            warn.append(f"SMD on '{s}' is assembled but no stencil (stencil.force='{force}')")

    # via treatment cross-check (config string vs extracted KiCad flags)
    vt = cfg.get("fab", {}).get("via_treatment", "any").lower()
    filled = tent["filling"] or any(tent["plugging"])
    if vt not in ("any", ""):
        if "fill" in vt and not filled:
            warn.append(f"via_treatment='{vt}' but KiCad has no plugging/filling set")
        if "tent" in vt and not any(tent["tenting"]):
            warn.append(f"via_treatment='{vt}' but KiCad tenting is off")
    return {"assembly": asm, "stencil": stencil, "warn": warn}

def via_summary(tent):
    parts = []
    if all(tent["tenting"]):
        parts.append("tented both sides")
    elif any(tent["tenting"]):
        parts.append("tented " + ("top" if tent["tenting"][0] else "bottom"))
    else:
        parts.append("not tented")
    if tent["filling"]:
        parts.append("filled")
    if any(tent["plugging"]):
        parts.append("plugged")
    return ", ".join(parts)

# ----------------------------- emit -----------------------------
def _dec(x):
    return str(x).replace(",", ".")          # KiCad job file uses locale decimal comma

def _color(gbr, fab):
    c = fab.get("soldermask_color", "any")    # prefer the human name from config
    return c if c not in ("any", "") else (gbr["soldermask_color"] or "any")

def emit_spec(cfg, gbr, tent, counts, res):
    sz = gbr["size"]
    fab = cfg.get("fab", {})
    req = cfg.get("requirements", {})
    L = []
    L += ["# GENERATED -- do not hand-edit. Resolved from the KiCad design + release.toml.",
          "# Commit this; CI 'release --drift' fails if it stops matching (catches accidental",
          "# layer-count / assembly-side / via / stackup changes). Comments = source.", ""]
    L += ["[board]",
          f'size_mm          = "{sz.get("X","?")} x {sz.get("Y","?")}"   # Edge.Cuts centerline (.gbrjob bbox - {gbr.get("edge_width",0)}mm stroke)',
          f'thickness_mm     = {gbr["thickness"]}   # .gbrjob -- Board Setup > Physical Stackup',
          f'layer_count      = {gbr["layers"]}   # .gbrjob LayerNumber -- Board Setup > Physical Stackup',
          f'soldermask_color = "{_color(gbr, fab)}"   # release.toml [fab] (or .gbrjob MaterialStackup)',
          f'surface_finish   = "{gbr["finish"] or fab.get("surface_finish","any")}"   # .gbrjob Finish (else release.toml [fab])',
          f'impedance        = "{"controlled" if gbr["impedance"] else fab.get("impedance_control","none")}"   # .gbrjob ImpedanceControlled',
          ""]
    L += ["[via]",
          f'treatment = "{via_summary(tent)}"   # .kicad_pcb tenting/plugging/filling -- Board Setup > Solder Mask/Paste',
          ""]
    L += ["[assembly]",
          f'sides         = {json.dumps(res["assembly"])}   # from release.toml assembly.sides (auto->derived from placement)',
          f'smd_top       = {counts["smd"]["top"]}   # pick&place (--smd-only --exclude-dnp)',
          f'smd_bottom    = {counts["smd"]["bottom"]}',
          f'tht_top       = {counts["tht"]["top"]}   # pick&place (through-hole, --exclude-dnp)',
          f'tht_bottom    = {counts["tht"]["bottom"]}',
          f'stencil_sides = {json.dumps(res["stencil"])}   # auto: SMD sides being assembled (or stencil.force)',
          ""]
    L += ["[stackup]",
          f'follow = "{cfg.get("stackup",{}).get("follow","any-standard")}"   # release.toml -- how tightly fab must match',
          "# layers below: .gbrjob MaterialStackup -- Board Setup > Physical Stackup"]
    n = 0
    for m in gbr["copper"]:
        n += 1
        L.append(f'[[stackup.layer]]  # copper')
        L.append(f'  n = {n}; name = "{m.get("Name")}"; copper_um = {round(float(m.get("Thickness",0))*1000)}')
    for m in gbr["dielectric"]:
        L.append(f'[[stackup.dielectric]]')
        L.append(f'  material = "{m.get("Material")}"; thickness_mm = {_dec(m.get("Thickness"))}; er = {_dec(m.get("DielectricConstant"))}; tan_d = {_dec(m.get("LossTangent"))}')
    L += ["", "[requirements]",
          f'rohs    = {str(bool(req.get("rohs", True))).lower()}   # release.toml [requirements]',
          f'ul94_v0 = {str(bool(req.get("ul94_v0", True))).lower()}   # release.toml [requirements]',
          ""]
    return "\n".join(L) + "\n"

def emit_readme(cfg, gbr, tent, counts, res):
    sz = gbr["size"]
    fab = cfg.get("fab", {})
    req = cfg.get("requirements", {})
    special = fab.get("special_layers", []) or ["(none)"]
    asm = res["assembly"] or ["(bare PCB, no assembly)"]
    L = ["# Manufacturing specification", "",
         "GENERATED from the KiCad design + release.toml. Review before ordering.", "",
         "## PCB",
         f"- Size: {sz.get('X','?')} x {sz.get('Y','?')} mm  (Edge.Cuts centerline)",
         f"- Layers: {gbr['layers']}",
         f"- Board thickness: {gbr['thickness']} mm",
         f"- Surface finish: {gbr['finish'] or fab.get('surface_finish','any')}",
         f"- Soldermask colour: {_color(gbr, fab)}",
         f"- Silkscreen colour: {fab.get('silkscreen_color','any')}",
         f"- Impedance control: {'controlled -- ' + fab.get('impedance_control','') if gbr['impedance'] else fab.get('impedance_control','none')}",
         f"- Vias: {via_summary(tent)}",
         f"- Copper: " + ", ".join(f"{round(float(m.get('Thickness',0))*1000)}um {m.get('Name')}" for m in gbr["copper"]),
         f"- Dielectric: " + "; ".join(f"{m.get('Material')} {_dec(m.get('Thickness'))}mm (er {_dec(m.get('DielectricConstant'))}, tand {_dec(m.get('LossTangent'))})" for m in gbr["dielectric"]),
         f"- Stackup strictness: {cfg.get('stackup',{}).get('follow','any-standard')}",
         f"- Special layers: {', '.join(special)}",
         f"- Flex: {fab.get('flex','none')}",
         "",
         "## Requirements",
         f"- RoHS: {'yes' if req.get('rohs', True) else 'no'}",
         f"- UL94 V-0: {'yes' if req.get('ul94_v0', True) else 'no'}",
         "",
         "## Assembly",
         f"- Sides: {', '.join(asm)}",
         f"- SMD: top {counts['smd']['top']}, bottom {counts['smd']['bottom']}",
         f"- THT: top {counts['tht']['top']}, bottom {counts['tht']['bottom']}",
         f"- Stencils: {', '.join(res['stencil']) or '(none)'}",
         ""]
    if fab.get("notes", "any") not in ("any", ""):
        L += [f"## Notes", fab["notes"], ""]
    return "\n".join(L) + "\n"

# ----------------------------- checks -----------------------------
REQUIRED_GERBERS = ["F_Cu", "B_Cu", "F_Mask", "B_Mask", "Edge_Cuts"]
def run_checks(outdir, res, pos_all, bom):
    problems = []
    have = " ".join(os.listdir(outdir))
    for g in REQUIRED_GERBERS:
        if g not in have:
            problems.append(f"missing gerber layer {g}")
    if not glob.glob(os.path.join(outdir, "*NPTH*")):
        problems.append("missing NPTH drill file")
    if not glob.glob(os.path.join(outdir, "*PTH*")):
        problems.append("missing PTH drill file")
    # pick&place >= BOM: every BOM part that should be placed has a placement
    if res["assembly"]:
        missing = sorted(bom - set(pos_all))
        if missing:
            problems.append(f"{len(missing)} BOM part(s) missing from pick&place: {', '.join(missing[:15])}")
    return problems

# ----------------------------- main -----------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project_dir", help="dir containing the .kicad_pro / .kicad_pcb / .kicad_sch")
    ap.add_argument("--config", default="release.toml")
    ap.add_argument("--spec", default="release_spec.toml", help="committed resolved-spec path (for drift)")
    ap.add_argument("--mode", choices=["build", "drift", "check", "pnp"], default="build")
    a = ap.parse_args()

    pd = a.project_dir.rstrip("/")
    pro = glob.glob(f"{pd}/*.kicad_pro")
    if not pro:
        sys.exit(f"release: no .kicad_pro in {pd}")
    stem = os.path.splitext(os.path.basename(pro[0]))[0]     # root sch/pcb share the project name
    pcb, sch = f"{pd}/{stem}.kicad_pcb", f"{pd}/{stem}.kicad_sch"
    cfg = load_toml(os.path.join(pd, a.config))

    if a.mode == "pnp":                 # light early gate: pos >= BOM only (no gerbers)
        work = tempfile.mkdtemp(prefix="pnp_")
        generate_pos(pcb, work)
        bom = bom_refs(sch)
        pos_all, _ = extract_pos(work)
        missing = sorted(bom - set(pos_all))
        for m in missing:
            print(f"::error::[pnp>=bom] {m}: in BOM but missing from pick&place")
        print(f"[pnp>=bom] {len(bom)} BOM parts, {len(pos_all)} placed, {len(missing)} missing")
        return 1 if missing else 0

    outdir = os.path.join(pd, "production") if a.mode == "build" else tempfile.mkdtemp(prefix="release_")
    generate(pcb, sch, outdir)
    gbr = extract_gbrjob(outdir)
    ew = extract_edge_width(outdir)                  # board is cut on the Edge.Cuts centerline
    if ew and isinstance(gbr["size"], dict):         # -> subtract the outline stroke from the bbox
        gbr["size"] = {k: round(v - ew, 3) for k, v in gbr["size"].items()}
    gbr["edge_width"] = ew
    tent = extract_tenting(pcb)
    bom = bom_refs(sch)
    pos_all, pos_smd = extract_pos(outdir)
    counts = count_assembly(pos_all, pos_smd, bom)
    res = resolve(cfg, gbr, tent, counts)

    spec = emit_spec(cfg, gbr, tent, counts, res)
    readme = emit_readme(cfg, gbr, tent, counts, res)

    for w in res["warn"]:
        print(f"::warning::[release] {w}")

    if a.mode == "drift":
        committed = os.path.join(pd, a.spec)
        old = open(committed).read() if os.path.isfile(committed) else ""
        if old != spec:
            print("::error::[release] release_spec.toml is stale -- regenerate and commit")
            return 1
        print("[release] spec up to date")
        return 0

    if a.mode == "build":
        open(os.path.join(pd, a.spec), "w").write(spec)
        open(os.path.join(outdir, "README-manufacturing.md"), "w").write(readme)

    problems = run_checks(outdir, res, pos_all, bom)
    for p in problems:
        print(f"::error::[release] {p}")
    print(f"[release] mode={a.mode}: {len(res['warn'])} warning(s), {len(problems)} error(s)")
    return 1 if problems else 0

if __name__ == "__main__":
    sys.exit(main())
