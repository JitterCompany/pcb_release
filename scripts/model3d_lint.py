#!/usr/bin/env python3
"""3D-model resolution gate for the STEP / render deliverables.

WHY: `kicad-cli pcb export step` and `pcb render` treat an unresolvable 3D model
as a WARNING -- they happily emit a STEP with the part silently absent, and a
render of a bare board. Customers use that STEP to check mechanical fit, so a
hollow one is worse than no build at all. This resolves every footprint's model
path the way KiCad does, BEFORE the export runs, and fails the release.

It also catches the classic CI-vs-desktop split: a designer's machine still has
${KICAD9_3DMODEL_DIR} (left over from an in-place KiCad upgrade) and private
aliases like ${JITTER} set in kicad_common.json, so everything renders locally
while a clean container resolves none of it.

Categories (errors gate, notes don't):
  unresolved   path still holds ${VAR} / :ALIAS: -- that var is not defined here
  missing      path resolved but no such file on disk
  unreadable   file is there but not readable -- kicad-cli then reports "Cannot
               identify actual file type", which reads like a corrupt model
  legacy_alias :ALIAS:file form -- GUI-only 3D search-path config; normalize it
               to ${ALIAS}/file so kicad-cli resolves it from the environment
  no_model     a footprint that WILL BE PLACED has no (visible) model. Scope is
               BOM membership, not a refdes list: exclude_from_bom / board_only
               footprints (test points, net ties, fiducials, logos) are excused
               because nothing gets soldered there, while a real part keeps its
               model mandatory -- so a *soldered* test point still gets checked.
               A note unless --require-model. --skip-no-model is a refdes-glob
               escape hatch for the odd part with genuinely no model available.
  hidden       model present but (hide yes): absent from STEP *and* renders
  dnp          model on a do-not-populate part -- informational (STEP is
               exported with --subst-models, DNP parts are still included)

Usage:
  model3d_lint.py BOARD.kicad_pcb [-D NAME=PATH]... [--require-model]
                  [--skip-no-model 'FID*,LOGO*,TP*'] [--verbose]

-D/--define adds to (and overrides) the process environment for substitution,
matching kicad-cli's own --define-var. KIPRJMOD always defaults to the board's
directory, as in KiCad.
"""
import argparse
import fnmatch
import os
import re
import sys

TOKEN = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')
VARREF = re.compile(r'\$\{([^}]*)\}|\$\(([^)]*)\)')       # KiCad accepts both forms
STOCKVAR = re.compile(r'^KICAD\d+_3DMODEL_DIR$')

# Where the stock model library lives, if the environment doesn't say. KiCad
# PREDEFINES KICAD<N>_3DMODEL_DIR internally (and keeps older N defined for
# backwards compatibility) -- they are usually absent from the process env, so
# without this every stock model would be reported unresolved while kicad-cli
# resolves it perfectly well.
STOCK_DIRS = ("/usr/share/kicad/3dmodels",           # distro / kicad docker image
              "/usr/local/share/kicad/3dmodels",     # source build
              "/app/share/kicad/3dmodels")           # flatpak


def stock_model_dir():
    return next((d for d in STOCK_DIRS if os.path.isdir(d)), None)


def _tokens(text):
    for m in TOKEN.finditer(text):
        yield m.group(0)


def _unquote(tok):
    if len(tok) >= 2 and tok[0] == '"':
        return re.sub(r'\\(.)', r'\1', tok[1:-1])
    return tok


def parse_footprints(text):
    """-> [nested-list] for every (footprint ...) directly under the root node.

    Two-pass so we never hold a tree for the whole (multi-MB) board: walk the
    token stream to find each footprint's token range, then build a tree for
    that range alone."""
    out, depth, buf = [], 0, None
    for tok in _tokens(text):
        if buf is not None:
            buf.append(tok)
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
            if buf is not None and depth == 1:            # footprint block closed
                out.append(_tree(buf))
                buf = None
        elif depth == 2 and tok == "footprint" and buf is None:
            buf = ["(", tok]                              # child of root -> a real footprint
    return out


def _tree(toks):
    stack, cur = [], []
    for tok in toks:
        if tok == "(":
            stack.append(cur); cur = []
        elif tok == ")":
            done, cur = cur, stack.pop()
            cur.append(done)
        else:
            cur.append(_unquote(tok))
    return cur[0] if len(cur) == 1 and isinstance(cur[0], list) else cur


def kids(node, name):
    return [c for c in node if isinstance(c, list) and c and c[0] == name]


def reference(fp):
    for p in kids(fp, "property"):
        if len(p) > 2 and p[1] == "Reference":
            return p[2]
    return "?"


def attrs(fp):
    return {a for node in kids(fp, "attr") for a in node[1:] if isinstance(a, str)}


def models(fp):
    """-> [(path, hidden)] in board order."""
    out = []
    for m in kids(fp, "model"):
        if len(m) < 2 or not isinstance(m[1], str):
            continue
        hidden = any(h[1:2] == ["yes"] for h in kids(m, "hide"))
        out.append((m[1], hidden))
    return out


def resolve(path, vars_):
    """Apply KiCad's model-path resolution -> (abs_path_or_None, kind).

    kind: 'ok' | 'legacy_alias' (+ '_unresolved' when a var stayed undefined, so
    a legacy path is reported as legacy rather than as a plain missing var)."""
    kind = "ok"
    if path.startswith(":"):                              # legacy ':ALIAS:relative/file'
        kind = "legacy_alias"
        alias, _, rest = path[1:].partition(":")
        path = "${%s}/%s" % (alias, rest) if rest else path

    def sub(m):
        return vars_.get(m.group(1) or m.group(2), m.group(0))

    prev = None
    while prev != path:                                   # a var value may itself hold a var
        prev, path = path, VARREF.sub(sub, path)
    if VARREF.search(path):
        return None, ("unresolved" if kind == "ok" else "legacy_alias_unresolved")
    if not os.path.isabs(path):                           # relative -> project dir, as KiCad does
        path = os.path.join(vars_.get("KIPRJMOD", "."), path)
    return os.path.normpath(path), kind


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board", help="*.kicad_pcb")
    ap.add_argument("-D", "--define", action="append", default=[], metavar="NAME=PATH",
                    help="define/override a path variable (like kicad-cli --define-var)")
    ap.add_argument("--require-model", action="store_true",
                    help="a footprint without any visible 3D model is an error, not a note")
    ap.add_argument("--skip-no-model", default="",
                    help="comma-separated refdes globs excused from --require-model (e.g. 'FID*,LOGO*')")
    ap.add_argument("--no-file-check", action="store_true",
                    help="check path hygiene only (vars defined, no legacy aliases, every BOM "
                         "part has a model) without requiring the model files to be present")
    ap.add_argument("--print-needed", metavar="VAR",
                    help="print nothing but the paths this board needs under ${VAR}, relative to "
                         "it, one per line, then exit -- feed to 'git sparse-checkout set --no-cone' "
                         "to fetch exactly those models and no more")
    ap.add_argument("--verbose", action="store_true", help="list every resolved model")
    a = ap.parse_args()

    board = os.path.abspath(a.board)
    vars_, defaulted = dict(os.environ), set()
    vars_.setdefault("KIPRJMOD", os.path.dirname(board))
    for d in a.define:
        k, _, v = d.partition("=")
        vars_[k] = v

    text = open(board, encoding="utf-8", errors="replace").read()
    fps = parse_footprints(text)
    if not fps:
        print("::error::[3d-lint] no footprints parsed -- is this a .kicad_pcb?")
        return 1

    # Stand in for KiCad's built-in KICAD<N>_3DMODEL_DIR definitions (see STOCK_DIRS).
    # Explicit -D still wins; only names the board actually uses are filled in, so a
    # board with no stock models never depends on finding the library.
    stock = stock_model_dir()
    for m in VARREF.finditer(text):
        name = m.group(1) or m.group(2)
        if STOCKVAR.match(name or "") and not vars_.get(name) and stock:
            vars_[name] = stock
            defaulted.add(name)

    if a.print_needed:
        # Both spellings of the same library: '${VAR}/file' and the legacy ':VAR:file'.
        # Emitted even for paths this run would flag, so the fetch stays complete while
        # the board is being cleaned up.
        pats = {"${%s}/" % a.print_needed: None, "$(%s)/" % a.print_needed: None,
                ":%s:" % a.print_needed: None}
        needed = set()
        for fp in fps:
            for path, _ in models(fp):
                for pre in pats:
                    if path.startswith(pre):
                        needed.add(path[len(pre):].lstrip("/"))
        for n in sorted(needed):
            print(n)
        return 0

    skips = [g.strip() for g in a.skip_no_model.split(",") if g.strip()]
    err, note, undefined, dnp_refs = [], [], {}, []
    seen_ok, missing_files, used_vars = 0, set(), set()

    for fp in sorted(fps, key=reference):
        ref, at = reference(fp), attrs(fp)
        ms = models(fp)
        visible = [(p, h) for p, h in ms if not h]

        if not visible:
            # In the BOM == it gets placed == it must show up in the STEP. Anything
            # excluded from the BOM (test point, net tie, fiducial, logo) or marked
            # board_only is not a soldered part, so a missing model is expected.
            if "exclude_from_bom" in at or "board_only" in at:
                continue
            msg = f"{ref}: in BOM (will be placed) but has no 3D model" + \
                  (" -- its only model is hidden" if ms else "")
            if a.require_model and not any(fnmatch.fnmatch(ref, g) for g in skips):
                err.append(("no_model", msg))
            else:
                note.append(("no_model", msg))
            continue

        for path, _ in visible:
            for m in VARREF.finditer(path):
                used_vars.add(m.group(1) or m.group(2))
            real, kind = resolve(path, vars_)
            if kind.startswith("legacy_alias"):            # the form is the defect; don't also
                alias = path[1:].partition(":")[0]         # report its var as undefined
                used_vars.add(alias)
                err.append(("legacy_alias", f"{ref}: legacy ':ALIAS:' model path '{path}' -- "
                                            f"rewrite as '${{{alias}}}/...' so kicad-cli resolves it "
                                            f"from the environment like every other path"))
                if real is None:
                    continue
            elif real is None:
                # One undefined variable is one root cause, however many parts use
                # it -- collect the refs and report it once, at the end.
                for m in VARREF.finditer(path):
                    undefined.setdefault(m.group(1) or m.group(2), set()).add(ref)
                continue
            if not a.no_file_check:
                # Readability matters as much as existence: kicad-cli reports an
                # unreadable model as "Cannot identify actual file type", which
                # looks like a corrupt file rather than a permissions problem.
                if not os.path.isfile(real):
                    err.append(("missing", f"{ref}: 3D model not found: {real}  (from '{path}')"))
                    missing_files.add(real)
                    continue
                if not os.access(real, os.R_OK):
                    err.append(("unreadable", f"{ref}: 3D model not readable "
                                              f"(mode {oct(os.stat(real).st_mode)[-3:]}): {real}"))
                    missing_files.add(real)
                    continue
            seen_ok += 1
            if a.verbose:
                print(f"[3d-lint]   {ref:8s} {real}")

        for path, hidden in ms:
            if hidden:
                note.append(("hidden", f"{ref}: model hidden, omitted from STEP and renders: {path}"))
        if "dnp" in at:
            dnp_refs.append(ref)

    # One line for the whole DNP set: individually they were pure noise, and what
    # they land in depends on the release config, not on this board.
    if dnp_refs:
        note.append(("dnp", f"{len(dnp_refs)} DNP part(s) ({', '.join(sorted(dnp_refs))}) -- kept out "
                            f"of the STEP when release.toml [customer] step_exclude_dnp is true; "
                            f"always present in renders (kicad-cli has no render DNP filter)"))

    # Undefined variables last, as one error each: a wall of per-refdes lines for
    # a single unset variable buries the one thing you have to fix.
    for var in sorted(undefined):
        refs = sorted(undefined[var])
        shown = ", ".join(refs[:8]) + (f", +{len(refs) - 8} more" if len(refs) > 8 else "")
        err.append(("unresolved", f"${{{var}}} is not defined -- {len(refs)} footprint(s) "
                                  f"cannot resolve their 3D model ({shown}). Define it via the "
                                  f"project's own entry point (which knows where that library "
                                  f"lives and can fetch it), or pass -D {var}=/path/to/models."))

    for kind, msg in note:
        print(f"::notice::[3d-lint] ({kind}) {msg}")
    for kind, msg in err:
        print(f"::error::[3d-lint] ({kind}) {msg}")

    if used_vars:
        print("[3d-lint] path variables used by this board:")
        for v in sorted(used_vars):
            val = vars_.get(v)
            how = "  (KiCad built-in default)" if v in defaulted else ""
            print(f"[3d-lint]   ${{{v}}} = {val if val else '<< NOT DEFINED >>'}{how}")
    scope = "path hygiene only, files not checked" if a.no_file_check else "resolved to a real file"
    print(f"[3d-lint] {len(fps)} footprints, {seen_ok} model(s) OK ({scope}), "
          f"{len(err)} error(s), {len(note)} note(s)")
    if missing_files:
        print(f"[3d-lint] {len(missing_files)} distinct model file(s) missing")
    return 1 if err else 0


if __name__ == "__main__":
    sys.exit(main())
