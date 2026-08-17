#!/usr/bin/env python3
"""Config-driven PCB release  (generic — destined for common/kicad_pcb_release; no project specifics).

Inverts the old interactive release_pcb.py: `release.toml` declares INTENT +
requirements + stackup strictness; CI GENERATES the matching manufacturing
outputs, then EXTRACTS facts from the structured exports (never regexes the
board file, except the small via-tenting block that no export carries), RESOLVES
them against the config, and emits:

  * release_spec.toml            -- committed, drift-checked (like pinmap.toml).
                                    Each value carries a terse provenance comment.
  * production/README-manufacturing.txt  -- the fab-spec checklist.

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
import csv
import datetime
import glob
import json
import os
import re
import shutil
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

def validate_config(cfg):
    """Fail fast on invalid / contradictory config, before any export."""
    su = cfg.get("stackup", {})
    spec = su.get("spec", "standard")
    if spec not in ("standard", "impedance", "controlled"):
        print(f"::error::[release] [stackup] spec must be standard|impedance|controlled, got '{spec}'")
        sys.exit(1)
    if spec == "standard" and str(su.get("impedance_note", "")).strip():
        print("::error::[release] [stackup] spec='standard' but impedance_note is set -- "
              "use spec='impedance' (or clear impedance_note)")
        sys.exit(1)

# ----------------------------- kicad-cli helpers -----------------------------
# kicad-cli is chatty on success, so we capture rather than stream -- but we must
# never DISCARD it: an unresolvable 3D model is reported on stdout with exit 0,
# and a failing export used to leave us with no diagnostics at all.
# kicad-cli has three separate wordings for "this part won't be in the output":
# the path didn't resolve, the file isn't there, or the file is there but is not
# readable / not a model it can parse. All three mean a hollow deliverable.
MODEL_FAIL = re.compile(r"Could not add 3D model for \S+|"
                        r"^File not found:|"
                        r"^Cannot identify actual file type for|"
                        r"^No model for filename")

def cli(*args):
    """Run kicad-cli -> combined stdout+stderr. Raises on non-zero, printing the
    captured output first so CI shows WHY the export failed."""
    p = subprocess.run(["kicad-cli", *args], capture_output=True, text=True)
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        print(f"::error::[release] kicad-cli {' '.join(args[:3])} failed (exit {p.returncode})")
        for line in out.splitlines():
            print(f"    | {line}")
        raise subprocess.CalledProcessError(p.returncode, ["kicad-cli", *args], out)
    return out

def model_failures(out):
    """-> [str] kicad-cli's own complaints about 3D models it could not load.
    The 3d-lint gate should have caught these already; this is the backstop that
    also covers whatever the linter's own path resolution gets wrong."""
    return [l.strip() for l in out.splitlines() if MODEL_FAIL.match(l.strip())]

def generate_pos(pcb, outdir):
    """Just the pick&place CSVs (all placed + SMD-only), DNP excluded -- light,
    no gerbers, for the early pos>=BOM check."""
    os.makedirs(outdir, exist_ok=True)
    cli("pcb", "export", "pos", "--format", "csv", "--units", "mm", "--side", "both",
        "--exclude-dnp", "-o", outdir + "/all-pos.csv", pcb)
    cli("pcb", "export", "pos", "--format", "csv", "--units", "mm", "--side", "both",
        "--exclude-dnp", "--smd-only", "-o", outdir + "/smd-pos.csv", pcb)

def generate_gerbers(pcb, outdir, layers):
    """Export ONLY the required gerber layers (+ drill). An explicit --layers list
    avoids kicad-cli's default dump of adhesive/courtyard/user/margin layers no fab
    needs -- matching what the manual process would hand-export.

    kicad-cli SILENTLY IGNORES an unknown/disabled layer name, so we count the
    gerbers actually produced and fail if it doesn't equal the layers requested --
    a missing fab layer must never slip through."""
    os.makedirs(outdir, exist_ok=True)
    before = set(os.listdir(outdir))
    cli("pcb", "export", "gerbers", "--layers", ",".join(layers), "-o", outdir + "/", pcb)
    produced = [f for f in os.listdir(outdir) if f not in before and not f.endswith(".gbrjob")]
    if len(produced) != len(layers):
        print(f"::error::[release] exported {len(produced)} gerbers but requested {len(layers)} "
              f"layers ({', '.join(layers)}) -- a layer was rejected/silently dropped by kicad-cli")
        sys.exit(1)
    cli("pcb", "export", "drill", "--excellon-separate-th", "-o", outdir + "/", pcb)

def board_layers(pcb):
    """Parse the board's enabled layers -> (copper_canonical_in_order, resolve),
    where resolve(name) maps a friendly layer name (canonical OR user name; '.'/'_'
    and case-insensitive, gerber extension tolerated) to its canonical --layers
    token, or None."""
    m = re.search(r"\(layers\s*(.*?)\n\t\)", open(pcb).read(), re.S)
    canon2user = {}
    for lm in re.finditer(r'\(\d+ "([^"]+)" \w+(?:\s+"([^"]+)")?\)', m.group(1)):
        canon2user[lm.group(1)] = lm.group(2) or lm.group(1)
    def cu_key(c):
        if c == "F.Cu": return -1
        if c == "B.Cu": return 1 << 30
        mm = re.match(r"In(\d+)\.Cu", c); return int(mm.group(1)) if mm else 1 << 20
    copper = sorted([c for c in canon2user if c.endswith(".Cu")], key=cu_key)
    norm = lambda s: re.sub(r"[^a-z0-9]", "", re.sub(r"\.(gbr|gm\d|g[a-z][a-z0-9]*)$", "", s.lower()))
    lut = {}
    for canon, usr in canon2user.items():
        lut.setdefault(norm(canon), canon); lut.setdefault(norm(usr), canon)
    return copper, (lambda name: lut.get(norm(name)))

def required_layers(cfg, resolve, copper, asm_sides, stencil_sides):
    """The layers a fab actually needs, from the config (like the manual script's
    categories): all copper + both mask/silk + Edge.Cuts, paste per STENCIL side,
    fab per ASSEMBLY side, plus configured special layers. Unknown special layers
    warn (never silently drop)."""
    L = list(copper) + ["F.Mask", "B.Mask", "F.SilkS", "B.SilkS", "Edge.Cuts"]
    if "top" in stencil_sides: L.append("F.Paste")
    if "bottom" in stencil_sides: L.append("B.Paste")
    if "top" in asm_sides: L.append("F.Fab")
    if "bottom" in asm_sides: L.append("B.Fab")
    for name in cfg.get("fab", {}).get("special_layers", []):
        tok = resolve(name)
        if not tok:
            print(f"::error::[release] special layer '{name}' (release.toml [fab]) is not on the board")
            sys.exit(1)
        L.append(tok)
    # if the stackup spec involves impedance, auto-add the board's marked impedance
    # gerber -- a user layer named ~"Impedance" (list it in special_layers instead
    # if yours has a different name).
    if cfg.get("stackup", {}).get("spec", "standard") in ("impedance", "controlled"):
        imp_layer = resolve("Impedance")
        if imp_layer:
            L.append(imp_layer)
    return list(dict.fromkeys(L))    # dedup, preserve order (impedance layer may repeat a special layer)

_PASSIVE_METRIC = re.compile(r"(^.*\s+[0-9]+)\s+[0-9]+[Mm]etric$")
def translate_fp(fp):
    """Footprint -> readable form (same rules as Kicad_bom_sync/translate_fp): drop
    the library prefix, '_' -> space, drop the metric-size suffix on passives.
    E.g. 'Capacitor_SMD:C_0603_1608Metric' -> 'C 0603'."""
    if not fp:
        return ""
    fp = str(fp).partition(":")[2] or str(fp)
    fp = fp.replace("_", " ").strip()
    m = _PASSIVE_METRIC.match(fp)
    return m.group(1) if m else fp

def _translate_bom_footprints(csv_path):
    """Rewrite the Footprint column of a kicad-cli BOM CSV to the readable form."""
    with open(csv_path, newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return
    hdr = [h.strip().lower() for h in rows[0]]
    if "footprint" not in hdr:
        return
    fi = hdr.index("footprint")
    for r in rows[1:]:
        if len(r) > fi:
            r[fi] = translate_fp(r[fi])
    with open(csv_path, "w", newline="") as f:
        csv.writer(f).writerows(rows)

def generate_bom(sch, out_csv, readable_footprints=True):
    """Grouped assembly BOM (CSV) via kicad-cli, DNP excluded. Same columns/order
    as Kicad_bom_sync's xlsx (BOM.py header_names) minus its 'Sync' column, so the
    CSV diffs cleanly against the sync BOM. readable_footprints post-processes the
    Footprint column like Kicad_bom_sync (readable + minimal v1/v2 diff)."""
    cli("sch", "export", "bom", "--exclude-dnp",
        "--group-by", "Value,Footprint",
        "--fields", "Reference,Footprint,Value,rating,QUANTITY,Manufacturer,MPN,Farnell,Mouser,Digikey",
        "--labels", "Ref,Footprint,Value,Rating,Qty,Manufacturer,MPN,Farnell,Mouser,Digikey",
        "-o", out_csv, sch)
    if readable_footprints:
        _translate_bom_footprints(out_csv)

def generate_customer(pcb, sch, cdir, stem, exclude_dnp=True):
    """CUSTOMER deliverables (NOT the fab zip): STEP, schematic PDF, top/bottom 3D
    renders, and -- if $KICAD_IBOM_DIR points at InteractiveHtmlBom -- an
    interactive HTML BOM.

    The STEP is what a customer fit-checks their enclosure against, so it must be
    COMPLETE or absent -- never quietly hollow. kicad-cli disagrees: a model it
    cannot resolve is one stdout line and exit 0, and `pcb render` does not even
    say that much (measured: silent, exit 0, components simply missing from the
    image). So both are hard failures here, and the renders are no longer
    best-effort -- a render that can't be produced is a release defect, not a
    nice-to-have. Only the iBOM stays optional (heavier deps, not a deliverable
    anyone dimensions against).

    exclude_dnp puts `--no-dnp` on the STEP so it shows the board AS ASSEMBLED,
    consistent with the pick&place and BOM which already drop DNP parts. NOTE the
    asymmetry: `pcb render` has no DNP filter at all, so DNP bodies always appear
    in the PNGs. The STEP is the dimensional deliverable, so that is where this
    matters; the renders stay illustrative."""
    if os.path.isdir(cdir):
        shutil.rmtree(cdir)
    os.makedirs(cdir)
    step = ["pcb", "export", "step", "--subst-models"] + (["--no-dnp"] if exclude_dnp else [])
    print(f"[release] STEP: DNP parts {'excluded (as assembled)' if exclude_dnp else 'INCLUDED'}"
          f" -- renders always include them (kicad-cli has no render DNP filter)")
    out = cli(*step, "-o", f"{cdir}/{stem}.step", pcb)
    bad = model_failures(out)
    for line in bad:
        print(f"::error::[release] STEP export: {line}")
    if bad:
        os.remove(f"{cdir}/{stem}.step")                 # never ship a hollow STEP
        sys.exit(f"release: {len(bad)} 3D model(s) missing from the STEP -- see errors above")
    cli("sch", "export", "pdf", "-o", f"{cdir}/{stem}-schematic.pdf", sch)
    for side in ("top", "bottom"):
        cli("pcb", "render", "--side", side, "--quality", "high", "--background", "opaque",
            "-o", f"{cdir}/{stem}-render-{side}.png", pcb)
    ibom = os.environ.get("KICAD_IBOM_DIR", "")
    entry = os.path.join(ibom, "generate_interactive_bom.py")
    if ibom and os.path.isfile(entry):
        try:
            subprocess.run(["python3", entry, "--no-browser", "--dest-dir", cdir, pcb],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            print("::warning::[release] interactive HTML BOM failed -- skipped (needs KiCad's pcbnew python)")
    else:
        print("::notice::[release] interactive HTML BOM skipped (set $KICAD_IBOM_DIR to enable)")

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
    """-> {ref: side} from a kicad-cli pos CSV. Uses a real CSV reader: a value
    may contain commas (e.g. an LED valued 'Green, 570 nm'), which naive
    comma-splitting would shift into the wrong column (mis-reporting the side)."""
    out = {}
    if not os.path.isfile(path):
        return out
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return out
    hdr = [h.strip().lower() for h in rows[0]]
    ri = hdr.index("ref") if "ref" in hdr else 0
    si = hdr.index("side") if "side" in hdr else len(hdr) - 1
    for r in rows[1:]:
        if len(r) > max(ri, si):
            out[r[ri].strip()] = r[si].strip().lower()
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

def resolve(cfg, tent, counts):
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
          f'impedance        = "{cfg.get("stackup",{}).get("spec","standard")}"   # release.toml [stackup] spec (standard|impedance|controlled)',
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
          f'spec = "{cfg.get("stackup",{}).get("spec","standard")}"   # release.toml -- standard | impedance | controlled',
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

def _copper_summary(copper):
    ths = {round(float(m.get("Thickness", 0)) * 1000) for m in copper}
    if len(ths) == 1:
        return f"copper {next(iter(ths))}um on all {len(copper)} layers"
    return "copper " + ", ".join(f"{round(float(m.get('Thickness',0))*1000)}um {m.get('Name')}" for m in copper)

def _stackup_layers(gbr, with_dkdf=False):
    """Per-layer stackup as separate bullets: 'L1: 35um copper (F.Cu)', then the
    dielectric below it, etc., naming each copper gerber layer."""
    lines, diel = [], gbr["dielectric"]
    for i, cu in enumerate(gbr["copper"]):
        lines.append(f"- L{i+1}: {round(float(cu.get('Thickness',0))*1000)}um copper ({cu.get('Name')})")
        if i < len(diel):
            d = diel[i]
            extra = f" (Dk {_dec(d.get('DielectricConstant'))}, Df {_dec(d.get('LossTangent'))})" if with_dkdf else ""
            lines.append(f"- {_dec(d.get('Thickness'))}mm {d.get('Material')}{extra}")
    return lines

def _impedance_str(spec, note):
    """Impedance line. The tool auto-references the marked Impedance gerber, so the
    note stays purely about the targets."""
    if spec == "standard":
        return "none"
    tail = "marked on the Impedance gerber"
    return f"{note}; {tail}" if note else tail

def stackup_lines(cfg, gbr):
    """Stackup + impedance at ONE strictness -- config [stackup] spec:
      standard   -> any standard N-layer at the finished thickness; no impedance
      impedance  -> impedance targets binding; stackup reference-only (substitution ok)
      controlled -> exact dielectric build is binding"""
    su = cfg.get("stackup", {})
    spec, note = su.get("spec", "standard"), su.get("impedance_note", "")
    th, n = gbr["thickness"], gbr["layers"]
    cu_um = round(float(gbr["copper"][0].get("Thickness", 0)) * 1000) if gbr["copper"] else "?"
    mat = gbr["dielectric"][0].get("Material") if gbr["dielectric"] else "FR4"   # KiCad material string
    if spec == "standard":
        return ["## Stackup",
                f"- Any standard {n}-layer {mat}, finished {th} mm; copper {cu_um}um each layer.",
                "- Impedance control: none"]
    if spec == "impedance":
        return (["## Stackup", "",
                 "Reference stackup (informational, substitution permitted):"]
                + _stackup_layers(gbr, with_dkdf=False)
                + ["", "Requirements:",
                   f"- Finished thickness: {th} mm +/- 10%",
                   f"- Copper: {cu_um}um on each layer",
                   f"- Material: {mat}, Tg >= 150 C",
                   f"- Impedance: {_impedance_str(spec, note)}"])
    return (["## Stackup -- controlled (build exactly)", "",
             "Stackup (binding):"]
            + _stackup_layers(gbr, with_dkdf=True)
            + ["", "Requirements:",
               f"- Finished thickness: {th} mm +/- 10%",
               f"- Impedance: {_impedance_str(spec, note)}"])

def special_sections(fab):
    """Sections for special-process layers -- currently conformal coating (any
    special layer whose name contains 'coating')."""
    layers = fab.get("special_layers", []) or []
    coating = [l for l in layers if "coating" in l.lower()]
    other = [l for l in layers if "coating" not in l.lower()]
    out = []
    if coating:
        sides = []
        if any("top" in l.lower() for l in coating): sides.append("top")
        if any("bot" in l.lower() for l in coating): sides.append("bottom")
        out += ["## Conformal coating",
                f"Apply conformal coating to {' + '.join(sides) if sides else 'the board'}.",
                "Cover all components and pads EXCEPT the areas drawn on these gerber layers:"]
        out += [f"- {l}" for l in coating] + [""]
    if other:
        out += ["## Special layers"] + [f"- {l}" for l in other] + [""]
    return out

def emit_readme(cfg, gbr, tent, counts, res, stem):
    sz = gbr["size"]
    fab = cfg.get("fab", {})
    req = cfg.get("requirements", {})
    asm = res["assembly"] or ["(bare PCB, no assembly)"]
    flex = fab.get("flex", "none")
    pcb_type = "rigid FR-4" if flex in ("none", "") else flex   # -> "flex", "rigid-flex ..." otherwise
    L = ["Manufacturing specification", "=" * 27, "",
         "## PCB",
         f"- PCB type: {pcb_type}",
         f"- Size: {sz.get('X','?')} x {sz.get('Y','?')} mm  (Edge.Cuts centerline)",
         f"- Layers: {gbr['layers']}",
         f"- Board thickness: {gbr['thickness']} mm",
         f"- Surface finish: {gbr['finish'] or fab.get('surface_finish','any')}",
         f"- Soldermask colour: {_color(gbr, fab)}",
         f"- Silkscreen colour: {fab.get('silkscreen_color','any')}",
         f"- Vias: {via_summary(tent)}",
         ""]
    L += stackup_lines(cfg, gbr) + [""]
    L += special_sections(fab)
    L += ["## Other requirements",
          f"- RoHS: {'yes' if req.get('rohs', True) else 'no'}",
          f"- UL94 V-0: {'yes' if req.get('ul94_v0', True) else 'no'}",
          "",
          "## Assembly"]
    if res["assembly"]:
        fabref = {"top": "F.Fab", "bottom": "B.Fab"}
        for side in res["assembly"]:
            L.append(f"- {side.capitalize()} side: {counts['smd'][side]} SMD, {counts['tht'][side]} THT "
                     f"(components on {fabref[side]})")
        L += ["",
              f"Place only the parts listed in the BOM (BOM_{stem}.csv).",
              "Pick & place positions are in all-pos.csv; see smd-pos.csv for the SMD components only.",
              ""]
    else:
        L.append("- No assembly (bare PCB)")
        if res["stencil"]:               # a stencil is a separate deliverable for a bare-PCB order
            L.append(f"- Stencils: {', '.join(res['stencil'])}")
        L.append("")
    if fab.get("notes", "any") not in ("any", ""):
        L += ["## Notes", fab["notes"], ""]
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
    ap.add_argument("--mode", choices=["build", "drift", "check", "pnp", "docs"], default="build")
    a = ap.parse_args()

    pd = a.project_dir.rstrip("/")
    pro = glob.glob(f"{pd}/*.kicad_pro")
    if not pro:
        sys.exit(f"release: no .kicad_pro in {pd}")
    stem = os.path.splitext(os.path.basename(pro[0]))[0]     # root sch/pcb share the project name
    pcb, sch = f"{pd}/{stem}.kicad_pcb", f"{pd}/{stem}.kicad_sch"
    cfg = load_toml(os.path.join(pd, a.config))
    validate_config(cfg)

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

    if a.mode == "docs":                # customer deliverables (STEP/PDF/renders/iBOM), NOT the fab zip
        generate_customer(pcb, sch, os.path.join(pd, "customer"), stem,
                          cfg.get("customer", {}).get("step_exclude_dnp", True))
        print(f"[release] customer outputs in {pd}/customer/")
        return 0

    outdir = os.path.join(pd, "production") if a.mode == "build" else tempfile.mkdtemp(prefix="release_")
    if a.mode == "build" and os.path.isdir(outdir):     # preserve the previous release, regenerate clean
        stamp = datetime.datetime.now().replace(microsecond=0).isoformat()
        os.rename(outdir, f"{outdir}.bak-{stamp}")

    # pos + BOM -> counts -> resolved sides FIRST, so we can export only the layers
    # the config actually needs (paste per stencil side, fab per assembly side).
    generate_pos(pcb, outdir)
    bom = bom_refs(sch)
    pos_all, pos_smd = extract_pos(outdir)
    counts = count_assembly(pos_all, pos_smd, bom)
    tent = extract_tenting(pcb)
    res = resolve(cfg, tent, counts)
    copper, blresolve = board_layers(pcb)
    generate_gerbers(pcb, outdir, required_layers(cfg, blresolve, copper, res["assembly"], res["stencil"]))

    gbr = extract_gbrjob(outdir)
    ew = extract_edge_width(outdir)                  # board is cut on the Edge.Cuts centerline
    if ew and isinstance(gbr["size"], dict):         # -> subtract the outline stroke from the bbox
        gbr["size"] = {k: round(v - ew, 3) for k, v in gbr["size"].items()}
    gbr["edge_width"] = ew

    spec = emit_spec(cfg, gbr, tent, counts, res)
    readme = emit_readme(cfg, gbr, tent, counts, res, stem)

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
        open(os.path.join(outdir, "README-manufacturing.txt"), "w").write(readme)  # .txt: opens everywhere
        generate_bom(sch, os.path.join(outdir, f"BOM_{stem}.csv"),
                     cfg.get("bom", {}).get("readable_footprints", True))

    problems = run_checks(outdir, res, pos_all, bom)
    for p in problems:
        print(f"::error::[release] {p}")

    if a.mode == "build":                               # package production/ -> dated zip in the project dir
        zbase = os.path.join(pd, f"production__{stem}__{datetime.date.today().isoformat()}")
        zpath = shutil.make_archive(zbase, "zip", outdir)
        print(f"[release] packaged {os.path.basename(zpath)}  ({len(os.listdir(outdir))} files in production/)")

    print(f"[release] mode={a.mode}: {len(res['warn'])} warning(s), {len(problems)} error(s)")
    return 1 if problems else 0

if __name__ == "__main__":
    sys.exit(main())
