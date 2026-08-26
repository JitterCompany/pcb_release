#!/usr/bin/env python3
"""Fetch the Jitter 3D model library, print `JITTER=<path>` for the caller to export.

Footprints reference our shared models as ${JITTER}. KiCad resolves that from a
designer's personal config; a clean container has no such config, which is how CI
once produced a STEP and renders with the modem, antenna and connectors silently
missing. So we fetch it -- a blobless, sparse checkout of ONLY the models this
board references (list derived from the board via model3d_lint.py --print-needed,
so it tracks part changes by itself): ~20 files, ~20MB, ~2s.

Latest master, deliberately not pinned: the models are essentially append-only, so
pinning bought reproducibility nobody needed at the cost of a ref to maintain, a
cache to key, and a stale-checkout failure mode. Re-fetched every run for the same
reason -- always current, nothing to invalidate.

The STOCK KiCad library (${KICAD<N>_3DMODEL_DIR}) is NOT handled here. KiCad
predefines those variables, so the library is an environment prerequisite: install
kicad-packages3d, or use the `-full` KiCad container image. Fetching it would
duplicate the package manager and mask a half-provisioned image; model3d_lint.py
reports it clearly instead.

Any OTHER private ${VAR} a board might use is likewise not handled -- the gate
names it, and it can be added here if that ever becomes real.

Usage: model_libs.py PROJECT_DIR
"""
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import model3d_lint as M

HERE = os.path.dirname(os.path.abspath(__file__))
VAR = "JITTER"
REPO = "https://github.com/JitterCompany/KicadComponents.git"
SUBDIR = "3D"                       # path inside the repo that ${JITTER} maps to


def log(msg):
    print(msg, file=sys.stderr)


def run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True).returncode == 0


def fetch(board, dst):
    """Sparse-checkout just this board's models from REPO@master.

    -> "ok"    fetched
       "empty" the board references no shared models at all, which is NORMAL for a board
               built entirely from stock footprints. This used to share a False return
               with a genuine fetch failure, so such a board failed the whole run with no
               error line to show for it.
       "fail"  the fetch itself failed (network / access)."""
    # splitlines(), NOT split(): --print-needed emits one filename per line and a
    # model name may legitimately contain spaces ("VQFN-HR-08 2x2.step"). Splitting
    # on whitespace tore such a name into two nonexistent entries, so the real file
    # was never sparse-checked-out and the board failed 3D lint with a "missing"
    # model that was sitting in the library all along.
    want = [w.strip() for w in
            subprocess.run([sys.executable, os.path.join(HERE, "model3d_lint.py"),
                            board, "--print-needed", VAR],
                           capture_output=True, text=True).stdout.splitlines()
            if w.strip()]
    if not want:
        log(f"[models] board references no ${{{VAR}}} models -- nothing to fetch")
        return "empty"
    log(f"[models] fetching {len(want)} model(s) <- {REPO} @ master")
    shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(dst, exist_ok=True)
    ok = (run("git", "init", "--quiet", dst)
          and run("git", "-C", dst, "remote", "add", "origin", REPO)
          and run("git", "-C", dst, "sparse-checkout", "set", "--no-cone",
                  *[f"/{SUBDIR}/{w}" for w in want])
          and run("git", "-C", dst, "fetch", "--quiet", "--filter=blob:none",
                  "--depth=1", "origin", "master")
          and run("git", "-C", dst, "checkout", "--quiet", "FETCH_HEAD"))
    if not ok:
        log(f"::error::[models] could not fetch {REPO} (network? access?)")
        shutil.rmtree(dst, ignore_errors=True)
    return "ok" if ok else "fail"


def main():
    if len(sys.argv) != 2:
        log(__doc__)
        return 2
    proj = sys.argv[1]
    pcbs = [f for f in os.listdir(proj) if f.endswith(".kicad_pcb")]
    if not pcbs:
        log(f"::error::[models] no .kicad_pcb in '{proj}'")
        return 2

    rc = 0
    # An already-set $JITTER wins: never override a developer's own environment,
    # and it keeps the gate usable offline.
    path = os.environ.get(VAR, "")
    if path and os.path.isdir(path):
        log(f"[models] {VAR}: using the value already in the environment")
    else:
        path = os.path.join(proj, ".kicad-3d", SUBDIR)
        got = fetch(os.path.join(proj, pcbs[0]), os.path.join(proj, ".kicad-3d"))
        if got == "ok":
            print(f"{VAR}={os.path.abspath(path)}")
        elif got == "fail":
            rc = 1
        # "empty" is not a failure: there was simply nothing to fetch.
    log(f"[models] {VAR} = {os.path.abspath(path)}")

    # Stock library: reported, never fetched -- it is the environment's job.
    stock = M.stock_model_dir() or ("<< NOT INSTALLED -- use the `-full` KiCad "
                                    "container image, or install kicad-packages3d >>")
    log(f"[models] stock KiCad models: {stock}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
