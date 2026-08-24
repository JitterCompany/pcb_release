#!/usr/bin/env python3
"""Generate a firmware pin-map TOML from a KiCad schematic — for a human
reference and CI pin/timer/EXTI checks.

DESIGN: delegate ALL connectivity to kicad-cli. Never re-solve nets from wire
geometry (KiCad's netlister already does it, correctly, across versions).

Two netlists from KiCad's own connectivity engine:
  * ROOT  (`--net`)        : authoritative pin->net for the whole design.
  * SHEET (`--sheet-net`)  : the sub-sheet the ref lives on, exported STANDALONE.
        In isolation each net is named by its LOCAL label, so intent suffixes
        (_IRQ / _A) on the MCU sheet survive — whereas the ROOT netlist can drop
        them when a net spans sheets (KiCad picks one canonical name, e.g. a net
        shows as `RI`, not `CELLULAR_UART_RI_IRQ`). Both auto-export if omitted.

The ONLY thing read from the .kicad_sch is the symbol's STATIC pin table (port
name + alternate-function list) for the timer/ADC/EXTI capability columns — no
coordinates, no wires, no geometry.

CONFIG: everything project-specific lives in the project's pinmap.config.toml, so
this script stays generic (see load_config for the schema). Paths in it are
relative to that file. It is deliberately NOT part of release.toml -- a pin map is
a schematic-stage artifact, usually settled and consumed by firmware long before
layout, let alone a manufacturing release.

Usage: generate_pinmap.py ROOT.kicad_sch [--config pinmap.config.toml] [--ref U1]
                          [--net F] [--sheet-net F] [--out F] [--check]
"""
import re, sys, os, glob, argparse, subprocess
from collections import defaultdict
from fnmatch import fnmatchcase

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from release_ci import load_toml

a = argparse.ArgumentParser()
a.add_argument("sch"); a.add_argument("--ref", default="")
a.add_argument("--config", help="project pinmap.config.toml (see load_config)")
a.add_argument("--net", help="root netlist (kicadsexpr); auto-exported if omitted")
a.add_argument("--sheet-net", help="ref's sheet netlist, exported STANDALONE; auto if omitted")
a.add_argument("--out"); a.add_argument("--check", action="store_true")
a.add_argument("--drift", action="store_true",
               help="regenerate, then fail if the committed artifact differs")
a = a.parse_args()


def load_config(path):
    """Read a project pinmap.config.toml -> (cfg, reserved, groups).

        ref = "U1"                  # MCU designator
        out = "../pinmap.toml"      # generated artifact, relative to this file
        ignore = ["..."]            # warning substrings downgraded to notices

        [reserved]                  # peripherals the FIRMWARE owns
        TIM2 = ""                   # "" -> no pin may use it
        TIM4 = "ADC_SYNC"           # only that net may use it

        [groups]                    # TOML-section grouping; FIRST MATCH WINS, so
        SWDIO = "debug"             # declaration order matters (dicts keep it)
        "UART_SWD_*" = "debug"      # fnmatch glob

    Returns empty dicts with no config, so the script still runs bare."""
    cfg = load_toml(path) if path and os.path.isfile(path) else {}
    def key(k):                     # glob keys need quoting in TOML: "UART_*" = "debug"
        k = k.strip()
        return (k[1:-1] if len(k) >= 2 and k[0] in "\"'" and k[-1] == k[0] else k).upper()
    sec = lambda n: cfg[n] if isinstance(cfg.get(n), dict) else {}
    reserved = {key(k): (str(v).strip().upper() or None) for k, v in sec("reserved").items()}
    groups = [[key(k), str(v).strip().lower(), 0]
              for k, v in sec("groups").items() if key(k) and str(v).strip()]
    return cfg, reserved, groups


CFG, RESERVED, GROUPS = load_config(a.config)
_here = os.path.dirname(os.path.abspath(a.config)) if a.config else "."
a.ref = a.ref or str(CFG.get("ref", "U1"))
if not a.out and not a.check and CFG.get("out"):
    a.out = os.path.normpath(os.path.join(_here, str(CFG["out"])))

# RESERVED = {TIMx/SPIx/...: allowed_net or None} -- from the config's [reserved].
# A pin whose ACTIVE function lands on one is flagged, unless the entry names the
# sole allowed net (e.g. the pulse-gen timer's own output). Marked '!' in the map.
# an active pin function that denotes a specific peripheral resource (routes to
# exactly one pin), used for double-booking + reserved checks.
PERIPH = re.compile(r'^(TIM\d+_CH\d+N?|LPTIM\d+_\w+|SPI\d+_\w+|I2C\d+_\w+|'
                    r'US?ART\d+_\w+|LPUART\d+_\w+|SAI\d+_\w+|SDMMC\d+_\w+|'
                    r'ADC\d+_IN\d+|DAC\d+_\w+|OCTOSPI\w*_\w+|FMC_\w+|DCMI_\w+)$')
def periph_inst(func):                          # TIM8_CH1 -> TIM8 ; SPI1_SCK -> SPI1
    m = re.match(r'([A-Z]+\d+)', func or "")
    return m.group(1) if (func and PERIPH.match(func) and m) else None
def reserved_hit(func):                          # peripheral instance if reserved
    inst = periph_inst(func)
    return inst if inst in RESERVED else None

def export_netlist(src, dst):
    subprocess.run(["kicad-cli", "sch", "export", "netlist",
                    "--format", "kicadsexpr", "-o", dst, src], check=True)

# KiCad escapes characters that are special in its own net/label syntax before
# writing them to disk, so a schematic label "R/W" reaches us as "R{slash}W".
# Decode them, or the escape leaks verbatim into the map and into every warning.
KICAD_ESC = {"slash": "/", "backslash": "\\", "lt": "<", "gt": ">", "colon": ":",
             "quote": "'", "dblquote": '"', "dollar": "$", "brace": "{",
             "space": " ", "tab": "\t"}
def unescape_kicad(s):
    return re.sub(r'\{(\w+)\}', lambda m: KICAD_ESC.get(m.group(1), m.group(0)), s)

# The section key is BOTH a TOML bare key and the name a firmware engineer will
# type, so it must be [a-z0-9_]. A net like "MCU_USB_D+" or "DISP_R/W" would
# otherwise emit a key TOML cannot even parse -- the artifact looked fine and
# silently was not loadable. '+'/'-' carry signal polarity, so they map to the
# conventional _p/_n rather than being flattened to '_' like other punctuation.
# NOTE: _n is also this tool's active-low suffix (from a ~{} overbar); a net that
# is both active-low and negative-polarity collapses, which the collision check
# below reports rather than silently merging.
def toml_key(name):
    k = re.sub(r'\+', '_p', re.sub(r'-', '_n', name))
    k = re.sub(r'[^A-Za-z0-9_]', '_', k)
    k = re.sub(r'_{2,}', '_', k).strip('_').lower()
    return k or "net"


def parse_netlist(path, ref):
    """-> {pin_number: (net_short_name, assigned_pinfunction, full_net_name, pintype)} for `ref`.

    net_short_name is the display label — sheet path dropped, ~{...} overbar
    stripped — deliberately lossy for a readable TOML. full_net_name keeps the
    sheet-qualified path AND the overbar, so nets that merely share a local
    label stay DISTINCT for the double-drive check (e.g. /Microcontroller/RESET,
    /LTE Modem/RESET, and a temp-sheet active-low ~{RESET} are three nets, not
    one triple-driven net)."""
    nl = open(path).read()
    out = {}
    idx = [m.start() for m in re.finditer(r'\t\(net\b', nl)] + [len(nl)]
    for x, y in zip(idx, idx[1:]):
        nm = re.search(r'\(name "([^"]*)"', nl[x:y])
        if not nm: continue
        full = nm.group(1)                                     # sheet-qualified, overbar intact
        # local label; ~{X} overbar -> active-low "_N" suffix (survives to TOML)
        net = re.sub(r'~\{([^}]*)\}', r'\1_N', full.rsplit("/", 1)[-1])
        net = unescape_kicad(net)      # {slash} etc are KiCad's on-disk escaping
        for nd in re.finditer(
                rf'\(ref "{ref}"\)\s*\(pin "([^"]*)"\)(?:\s*\(pinfunction "([^"]*)"\))?'
                rf'(?:\s*\(pintype "([^"]*)"\))?', nl[x:y]):
            func = re.sub(r'_\d+$', '', nd.group(2)) if nd.group(2) else None
            out[nd.group(1)] = (net, func, full, nd.group(3) or "")
    return out

# --- locate the ref's sheet file + read its symbol (static pin table only) ---
sheet_path = libname = sheet_text = None
for f in glob.glob(os.path.join(os.path.dirname(a.sch) or ".", "*.kicad_sch")):
    s = open(f).read()
    for m in re.finditer(r'\(symbol\b', s):
        blk = s[m.start():m.start() + 3000]
        r = re.search(r'\(property "Reference" "([^"]+)"', blk)
        l = re.search(r'\(lib_id "([^"]+)"\)', blk)
        if r and l and r.group(1) == a.ref:
            sheet_path, libname, sheet_text = f, l.group(1), s; break
    if libname: break
if not libname: sys.exit(f"symbol for {a.ref} not found in any sheet")

i = sheet_text.find(f'(symbol "{libname}"'); d = 0
for j in range(i, len(sheet_text)):
    d += (sheet_text[j] == '(') - (sheet_text[j] == ')')
    if d == 0: e = j; break
lib = sheet_text[i:e + 1]
pinmeta, k = {}, 0                       # pin number -> (port name, [alternates])
while (k := lib.find('(pin ', k)) >= 0:
    dd = 0
    for m in range(k, len(lib)):
        dd += (lib[m] == '(') - (lib[m] == ')')
        if dd == 0: pe = m; break
    b = lib[k:pe + 1]
    nm = re.search(r'\(name "([^"]*)"', b); nu = re.search(r'\(number "([^"]*)"', b)
    if nm and nu: pinmeta[nu.group(1)] = (nm.group(1), re.findall(r'\(alternate "([^"]*)"', b))
    k = pe + 1

# --- the two netlists (KiCad's connectivity; auto-export if not supplied) ---
import tempfile
def _tmp(tag):
    fd, p = tempfile.mkstemp(prefix=f"pinmap_{tag}_", suffix=".net"); os.close(fd); return p
root_net = a.net or _tmp("root")
if not a.net: export_netlist(a.sch, root_net)
sheet_net = a.sheet_net or _tmp("sheet")
if not a.sheet_net: export_netlist(sheet_path, sheet_net)
pin2net = parse_netlist(root_net, a.ref)      # canonical (whole design)
pin2local = parse_netlist(sheet_net, a.ref)   # local labels (intent suffixes)

# Detect power supply nets.
# derived from which nets drive pins with these electrical types, never from their name.
POWER_TYPES = {"power_in", "power_out"}
power_nets = {v[2] for v in pin2net.values() if v[3] in POWER_TYPES}
power_pins = {n for n, v in pin2net.items() if v[2] in power_nets}

# --- records for connected, non-placeholder pins ---
# Primary net name comes from the ISOLATED-SHEET netlist, not the whole-design
# ROOT one: the MCU-sheet-local label is unique per pin and preserves MCU-side
# intent (RESET / CELLULAR_RESET / ADC_RESET_N — not three collapsed "RESET"s;
# CELLULAR_UART_RI_IRQ — not the far-sheet's canonical "RI"). ROOT is still the
# authoritative connected-pin set + active pinfunction.
# Intent suffixes: recognized to drive the EXTI/ADC columns, then STRIPPED from
# the emitted firmware name — the meaning already lives in those columns, so
# 'ABC_IRQ' emits as 'abc' (still flagged as needing EXTI) and 'PCB_TEMP_A' as
# 'pcb_temp'. Polarity '_n' (overbar) is NOT a matching suffix, so it's kept.
IRQ = ("_IRQ", "_INT", "_EXTI")
INTENT = IRQ + ("_A",)
def strip_intent(n):
    for s in INTENT:
        if n.endswith(s): return n[:-len(s)]
    return n
rec = []
full_by_num = {}                                   # pin number -> full sheet-local net id
for num, (rnet, func, rfull, _rt) in pin2net.items():
    if rnet.startswith("unconnected-"): continue
    port, alts = pinmeta.get(num, ("?", []))
    name, _lf, lfull, _lt = pin2local.get(num, (rnet, func, rfull, ""))  # sheet-local name + full id
    # rec[0] = emitted firmware name (suffix stripped); rec[5] = raw local name
    # (suffix intact) — the EXTI/analog matching below relies on the suffix.
    rec.append((strip_intent(name), port, alts, func, num, name))
    full_by_num[num] = lfull

timers = lambda al: [x for x in al if "TIM" in x and "_CH" in x]
def exti(port):
    m = re.match(r'P[A-H](\d+)$', port); return int(m.group(1)) if m else None
def adc_ch(alts, func):
    if func and re.match(r'ADC\d+_IN', func): return func
    a2 = [x for x in alts if re.match(r'ADC\d+_IN', x)]
    return a2[0] if a2 else None
# --- sanity checks ---
warn = []
# The power heuristic above rests entirely on the symbol being typed. If it is not,
# supply pins silently fall through into the map and drag the rail into the
# double-drive check -- say so rather than emitting a quietly wrong artifact.
if not power_nets:
    warn.append(f"no power_in/power_out pins found on {a.ref} -- is the symbol's pin "
                f"electrical type set? supply pins will be listed as if they were I/O")
for sig, want in [("SWDIO","PA13"),("SWCLK","PA14"),("LSE_P","PC14"),("LSE_N","PC15")]:
    got = [r[1] for r in rec if r[0] == sig]
    if got and want not in got: warn.append(f"{sig} on {got}, expected {want} (fixed-function)")
# double-drive: key on the FULL sheet-local net identity, not the display name,
# so two nets that merely share a local label can't false-positive. A net truly
# spanning U1 pins keeps one full id and is still caught.
netpins = defaultdict(list)
for r in rec:
    if r[4] in power_pins: continue         # supply pins share a rail by design
    netpins[full_by_num[r[4]]].append(r[1])
for n, ps in netpins.items():
    if len(ps) > 1: warn.append(f"net {n} on multiple pins {ps} (double-drive?)")
# suffix-stripping must not merge two DISTINCT nets into one firmware name
seen = {}
for r in rec:
    if r[0] in seen and full_by_num[seen[r[0]]] != full_by_num[r[4]]:
        warn.append(f"firmware name '{r[0].lower()}' collides after intent-suffix "
                    f"strip (pins {seen[r[0]]}+{r[4]}) -- rename one net")
    seen[r[0]] = r[4]
# ...and neither may sanitising to a legal TOML key (e.g. "A+" and "A-" both -> a_p/a_n
# are fine, but "A/B" and "A_B" both collapse to a_b)
kseen = {}
for r in rec:
    k = toml_key(r[0])
    if k in kseen and full_by_num[kseen[k]] != full_by_num[r[4]]:
        warn.append(f"TOML key '{k}' collides after sanitising the net name "
                    f"(pins {kseen[k]}+{r[4]}) -- rename one net")
    kseen[k] = r[4]
# EXTI collisions: intent from the SHEET-LOCAL net name (suffix-preserving).
irq, irq_pins = defaultdict(list), set()
for net, port, alts, func, num, local in rec:
    ln = exti(port)
    if ln is not None and local.endswith(IRQ):
        irq[ln].append((local, port)); irq_pins.add(num)
for ln, rs in irq.items():
    if len(rs) > 1: warn.append(f"EXTI{ln} collision (declared _IRQ): {[f'{n}={p}' for n,p in rs]}")
# peripheral DOUBLE-BOOKING: a peripheral signal routes to exactly one pin, so
# the same ACTIVE function on two pins is an error (uses the netlist pinfunction).
fpins = defaultdict(list)
for net, port, alts, func, num, local in rec:
    if func and PERIPH.match(func): fpins[func].append(f"{net}({port})")
for func, ps in fpins.items():
    if len(ps) > 1: warn.append(f"peripheral {func} assigned to multiple pins: {ps}")
# RESERVED peripheral used by a pin's ACTIVE function (unless the allowed net)
for net, port, alts, func, num, local in rec:
    inst = reserved_hit(func)
    if inst and RESERVED[inst] != net:
        allow = f" (only '{RESERVED[inst]}' allowed)" if RESERVED[inst] else " (firmware-internal)"
        warn.append(f"{net} ({port}) actively uses reserved {func}{allow}")
    if local.endswith("_A") and not adc_ch(alts, func):
        warn.append(f"{net} ({port}) is _A (analog) but pin has no ADC channel -- repin")

# --- emit TOML. The committed artifact (--out) is the CLEAN pin map only;
# warnings go to stdout/CI, never into the file, so the drift check compares the
# actual pin->net->func mapping, decoupled from transient warnings. ---
header = [f"# Auto-generated pin map for {a.ref} from {os.path.basename(a.sch)}",
     "# kicad-cli netlists (root + standalone sheet) — all connectivity from KiCad.",
     f"# Regenerate: generate_pinmap.py {os.path.basename(a.sch)} --ref {a.ref}",
     f"# Reserved peripherals ('!' = firmware-owned): {', '.join(sorted(RESERVED)) or '(none)'}",
     "# Net names are MCU-SHEET-LOCAL labels (unique, MCU-side intent preserved).",
     "# Sections = name's first '_'-token, unless set in tools/pinmap-groups.txt.",
     "# '=> FUNC' is the ACTIVE alternate set in KiCad; 'cap:' lists other options.",
     "# Intent suffixes _IRQ/_A are STRIPPED from the name — their meaning is the",
     "# 'EXTIn *IRQ*' / ADC annotation. _n = active-low (KiCad ~{} overbar) is kept.", ""]
# Section grouping: default = first '_'-token of the name; an explicit rule in
# --groups overrides it (first match wins). The TOML is a human reference, so
# grouping is curated in this reviewable file, NOT by hand-editing the artifact.
#   NAME     = group     exact net name
#   PREFIX*  = group      fnmatch glob (*, ?, [..]); matched case-insensitively
def group_of(name):                                # GROUPS from [pinmap.groups]
    up = name.upper()
    for rule in GROUPS:
        if fnmatchcase(up, rule[0]): rule[2] += 1; return rule[1]
    return name.split('_')[0].lower() if '_' in name else name.lower()

body, groups, power = [], defaultdict(list), []
for r in rec:
    (power if r[4] in power_pins else
     groups[group_of(r[0])]).append(r)
for pat, grp, hits in GROUPS:            # catch typo'd rules; non-gating (stderr)
    if not hits: print(f"[pinmap] note: group rule '{pat.lower()} = {grp}' matched no nets", file=sys.stderr)
for g in sorted(groups):
    body.append(f"[{g}]")
    for net, port, alts, func, num, local in sorted(groups[g]):
        active = func if (func and PERIPH.match(func)) else None   # a real peripheral, not the port
        cap = "/".join((c + '!' if periph_inst(c) in RESERVED else c) for c in timers(alts))
        ln = exti(port)
        parts = [port.lower()]
        if active: parts.append(f"=> {active}")
        if cap:    parts.append(f"cap: {cap}")
        if num in irq_pins:                      # EXTI line only relevant for declared IRQs
            parts.append(f"EXTI{ln} *IRQ*" if ln is not None else "*IRQ*")
        body.append(f'{toml_key(net)} = "{port.lower()}"  # {" | ".join(parts)}')
    body.append("")
if power:
    pc = defaultdict(int)
    for net, *_ in power: pc[net] += 1
    body += ["# power/ground:", "#  " + ", ".join(f"{n} x{c}" if c > 1 else n for n, c in sorted(pc.items()))]
wblock = (["# === SANITY WARNINGS ==="] + [f"#  ! {w}" for w in warn] + [""]) if warn else []
if a.out:
    open(a.out, "w").write("\n".join(header + body))          # clean artifact, no warnings
    print(f"wrote {a.out}: {len(rec)} pins, {len(warn)} warnings")
    if a.drift:
        # Committed artifact must match the schematic. Separate signal from --check:
        # this catches an implicit/breaking pinout change so a human re-reviews it.
        d = subprocess.run(["git", "diff", "--exit-code", "--", a.out],
                           capture_output=True, text=True)
        if d.returncode:
            print(f"::error::[pinmap] {a.out} is stale -- regenerate it and commit the result")
            print(d.stdout)
            sys.exit(1)
        print(f"[pinmap] {a.out} is up to date")
elif not a.check:
    print("\n".join(header + wblock + body))

# --- CI mode ---
if a.check:
    pats = [str(p).strip() for p in (CFG.get("ignore") or []) if str(p).strip()]
    active = [w for w in warn if not any(p in w for p in pats)]
    for w in warn:
        print(f"::{'notice' if w not in active else 'error'}::[pinmap"
              f"{' ignored' if w not in active else ''}] {w}")
    print(f"pinmap check ({a.ref}): {len(rec)} pins, {len(active)} error(s), "
          f"{len(warn)-len(active)} ignored")
    sys.exit(1 if active else 0)
