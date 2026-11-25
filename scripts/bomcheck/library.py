"""Where signed pin tables live: the board's repo, and the component library.

A signed table holds what a datasheet says about one MPN. It does not depend on
the board, so it belongs next to the symbols that use the part, in the shared
component library: a part or a design snippet taken from the library then
comes with its check. A part that only this board uses keeps its table in the
board's repo.

The library is found from the project's own sym-lib-table: a library whose
file lives in another git repository than the board (a submodule such as
libraries/KicadComponents) is a component library. No library name is
hard-coded.
"""
import os
import re
import subprocess

SIGNED = ("attestations", "reference", "datasheet")
# Signed interface tables: what an interface spec says each pin of a symbol
# is. Keyed to symbols, not MPNs, so they belong in the library too.
INTERFACE = ("attestations", "reference", "interface")


def _git_root(path):
    try:
        r = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=10)
    except Exception:                                            # noqa: BLE001
        return ""
    return os.path.realpath(r.stdout.strip()) if r.returncode == 0 else ""


def lib_table(project):
    """-> {nickname: absolute path} from the project's sym-lib-table. URIs with
    a variable other than ${KIPRJMOD} are left out: they point into a
    machine's own KiCad install."""
    path = os.path.join(project, "sym-lib-table")
    if not os.path.isfile(path):
        return {}
    out = {}
    text = open(path, encoding="utf-8").read()
    for name, uri in re.findall(r'\(lib \(name "([^"]+)"\).*?\(uri "([^"]+)"\)', text):
        uri = uri.replace("${KIPRJMOD}", os.path.abspath(project))
        if "${" in uri:
            continue
        out[name] = os.path.normpath(uri if os.path.isabs(uri)
                                     else os.path.join(project, uri))
    return out


def libraries(project):
    """-> [root] of component libraries: git repositories, other than the
    board's own, that hold a symbol library the project uses."""
    own = _git_root(os.path.abspath(project))
    roots = []
    for path in lib_table(project).values():
        root = _git_root(os.path.dirname(path)) if os.path.exists(path) else ""
        if root and root != own and root not in roots:
            roots.append(root)
    return roots


def component_library(project, override=""):
    """-> the one library to save shared tables in, or "" when there is none
    or it is not clear which (then --library or BOMCHECK_LIBRARY says)."""
    override = override or os.environ.get("BOMCHECK_LIBRARY", "")
    if override:
        return os.path.realpath(os.path.expanduser(override))
    roots = libraries(project)
    return roots[0] if len(roots) == 1 else ""


def read_roots(project):
    """-> [root] whose signed tables a board reads, after its own. The
    library named by --library comes first: it is the clone tables are signed
    into, newer than the copy the board pins as a submodule."""
    roots = libraries(project)
    extra = component_library(project)
    return ([extra] if extra else []) + [r for r in roots if r != extra]


def signed_dir(root):
    return os.path.join(root, *SIGNED)


def interface_dir(root):
    return os.path.join(root, *INTERFACE)


def scope_of(lib_id, project):
    """Where a part's table belongs by default: "project" for a symbol from a
    library inside the board's repo, "library" for one from a component
    library or from KiCad's own libraries (not in the project's table)."""
    nick = lib_id.partition(":")[0]
    path = lib_table(project).get(nick)
    if not path:
        return "library"
    own = _git_root(os.path.abspath(project))
    root = _git_root(os.path.dirname(path)) if os.path.exists(path) else ""
    return "project" if root == own else "library"


def uncommitted(root):
    """-> [relative path] of signed tables (datasheet and interface) in a
    library that git does not have yet: they reach other boards only through
    a commit there."""
    try:
        r = subprocess.run(["git", "-C", root, "status", "--porcelain",
                            "--untracked-files=all", "--", os.path.join(*SIGNED),
                            os.path.join(*INTERFACE)],
                           capture_output=True, text=True, timeout=10)
    except Exception:                                            # noqa: BLE001
        return []
    # A table deleted and written again shows twice (D and ??): count it once.
    return list(dict.fromkeys(line[3:] for line in r.stdout.splitlines()
                              if line.strip()))
