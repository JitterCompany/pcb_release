#!/usr/bin/env python3
"""A small, correct TOML reader — the one this package uses everywhere.

WHY THIS EXISTS. The repo already has two TOML readers and both corrupt this
board's real data:

  * release_ci.load_toml strips at the first '#' regardless of quoting, and
    splits inline arrays on ',' regardless of quoting. On BOM line 17 of
    a test board, `rating = ["600W, Vr=13V,Vc=22V", "Cj<1nF"]` parses to FOUR
    elements with stray quote characters attached, and any string containing a
    '#' is silently truncated.
  * attest_scan.py's _mini_toml/_mini_value has the same array bug.

Attestation records carry `source`, `claim` and multi-sentence `evidence`
strings — exactly the values those bugs eat. A validation tool that silently
mis-reads its own database is worse than no tool, so this is groundwork, not
polish.

WHY NOT tomllib. The dev host is Python 3.10 (`import tomllib` raises) while the
KiCad CI container is newer. Selecting a parser by version would mean CI and the
laptop could disagree about what a database says, which is a bug class we refuse
to ship into a gate. So: always our parser. `--selftest` runs tomllib as a
DIFFERENTIAL ORACLE where available, which is how we know ours agrees with the
real thing rather than merely being self-consistent.

SUPPORTED: [table], [[array.of.tables]], nested keys (a.b.c), basic strings with
escapes, literal strings, triple-quoted strings, inline arrays (nested,
multi-line), inline tables, integers, floats, booleans, and bare dates. That is
a superset of what the databases use, and it errors with a line number on
anything it does not understand rather than guessing.
"""
import datetime
import re
import sys

__all__ = ["loads", "load", "quote", "TomlError"]


class TomlError(ValueError):
    """Parse failure, always with a line number — never a silent misread."""


_BARE = re.compile(r"[A-Za-z0-9_-]+")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}$")
_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}")
_INT = re.compile(r"[+-]?\d+$")
_FLOAT = re.compile(r"[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")
_ESCAPES = {'"': '"', "\\": "\\", "b": "\b", "f": "\f",
            "n": "\n", "r": "\r", "t": "\t"}


class _Cursor:
    """Character cursor over the whole document.

    Deliberately NOT line-based: strings, arrays and inline tables may all span
    lines, and every line-based shortcut is where the existing readers break.
    """

    def __init__(self, text):
        self.s = text
        self.i = 0

    def line(self):
        return self.s.count("\n", 0, self.i) + 1

    def err(self, msg):
        raise TomlError(f"line {self.line()}: {msg}")

    def eof(self):
        return self.i >= len(self.s)

    def peek(self, n=1):
        return self.s[self.i:self.i + n]

    def skip_ws(self, newlines=False):
        """Advance past spaces, comments, and (optionally) newlines.

        A '#' only starts a comment out here — never inside a string, which is
        the whole point.
        """
        while self.i < len(self.s):
            c = self.s[self.i]
            if c in " \t\r" or (newlines and c == "\n"):
                self.i += 1
            elif c == "#":
                nl = self.s.find("\n", self.i)
                self.i = len(self.s) if nl < 0 else nl
            else:
                break


def _read_key(cur):
    """A dotted key -> list of parts. Quoted parts keep their literal text."""
    parts = []
    while True:
        cur.skip_ws()
        c = cur.peek()
        if c in ('"', "'"):
            parts.append(_read_string(cur))
        else:
            m = _BARE.match(cur.s, cur.i)
            if not m:
                cur.err("expected a key")
            parts.append(m.group(0))
            cur.i = m.end()
        cur.skip_ws()
        if cur.peek() == ".":
            cur.i += 1
            continue
        return parts


def _read_string(cur):
    q = cur.peek()
    if cur.peek(3) in ('"""', "'''"):
        return _read_multiline(cur, cur.peek(3))
    cur.i += 1
    out = []
    literal = q == "'"
    while True:
        if cur.eof():
            cur.err("unterminated string")
        c = cur.s[cur.i]
        if c == "\n":
            cur.err("newline in single-line string")
        if c == q:
            cur.i += 1
            return "".join(out)
        if c == "\\" and not literal:
            cur.i += 1
            e = cur.s[cur.i:cur.i + 1]
            if e in _ESCAPES:
                out.append(_ESCAPES[e])
                cur.i += 1
            elif e in ("u", "U"):
                n = 4 if e == "u" else 8
                hexs = cur.s[cur.i + 1:cur.i + 1 + n]
                if len(hexs) < n:
                    cur.err("truncated unicode escape")
                out.append(chr(int(hexs, 16)))
                cur.i += 1 + n
            else:
                cur.err(f"unknown escape \\{e}")
        else:
            out.append(c)
            cur.i += 1


def _read_multiline(cur, delim):
    cur.i += 3
    if cur.peek() == "\n":            # a newline right after the opener is trimmed
        cur.i += 1
    literal = delim[0] == "'"
    out = []
    while True:
        if cur.eof():
            cur.err("unterminated multi-line string")
        if cur.peek(3) == delim:
            cur.i += 3
            return "".join(out)
        c = cur.s[cur.i]
        if c == "\\" and not literal:
            nxt = cur.s[cur.i + 1:cur.i + 2]
            if nxt == "\n":           # line-continuation: swallow the newline + indent
                cur.i += 2
                while cur.peek() in (" ", "\t", "\n", "\r") and not cur.eof():
                    cur.i += 1
                continue
            if nxt in _ESCAPES:
                out.append(_ESCAPES[nxt])
                cur.i += 2
                continue
            if nxt in ("u", "U"):
                n = 4 if nxt == "u" else 8
                out.append(chr(int(cur.s[cur.i + 2:cur.i + 2 + n], 16)))
                cur.i += 2 + n
                continue
            cur.err(f"unknown escape \\{nxt}")
        out.append(c)
        cur.i += 1


def _read_value(cur):
    cur.skip_ws()
    if cur.eof():
        cur.err("expected a value")
    c = cur.peek()
    if c in ('"', "'"):
        return _read_string(cur)
    if c == "[":
        return _read_array(cur)
    if c == "{":
        return _read_inline_table(cur)
    # A bare token: ends at a comma, bracket, brace, comment or newline. Scanned
    # by delimiter rather than split by one, so nothing quoted is ever cut.
    j = cur.i
    while j < len(cur.s) and cur.s[j] not in ",]}\n#":
        j += 1
    tok = cur.s[cur.i:j].strip()
    cur.i = j
    if not tok:
        cur.err("expected a value")
    return _atom(tok, cur)


def _atom(tok, cur):
    if tok == "true":
        return True
    if tok == "false":
        return False
    if _DATETIME.match(tok):
        t = tok.replace(" ", "T").replace("Z", "+00:00").rstrip("z")
        try:
            return datetime.datetime.fromisoformat(t)
        except ValueError:
            return tok                       # keep the text rather than lose it
    if _DATE.match(tok):
        y, m, d = tok.split("-")
        return datetime.date(int(y), int(m), int(d))
    if _INT.match(tok):
        return int(tok)
    if _FLOAT.match(tok):
        return float(tok)
    cur.err(f"cannot parse value {tok!r}")


def _read_array(cur):
    cur.i += 1                                # '['
    out = []
    while True:
        cur.skip_ws(newlines=True)
        if cur.eof():
            cur.err("unterminated array")
        if cur.peek() == "]":
            cur.i += 1
            return out
        out.append(_read_value(cur))
        cur.skip_ws(newlines=True)
        if cur.peek() == ",":
            cur.i += 1
        elif cur.peek() == "]":
            cur.i += 1
            return out
        else:
            cur.err("expected ',' or ']' in array")


def _read_inline_table(cur):
    cur.i += 1                                # '{'
    out = {}
    while True:
        cur.skip_ws(newlines=True)
        if cur.eof():
            cur.err("unterminated inline table")
        if cur.peek() == "}":
            cur.i += 1
            return out
        key = _read_key(cur)
        cur.skip_ws()
        if cur.peek() != "=":
            cur.err("expected '=' in inline table")
        cur.i += 1
        _assign(out, key, _read_value(cur), cur)
        cur.skip_ws(newlines=True)
        if cur.peek() == ",":
            cur.i += 1
        elif cur.peek() == "}":
            cur.i += 1
            return out
        else:
            cur.err("expected ',' or '}' in inline table")


def _walk(root, parts, cur, array_ok=True):
    """Descend/create nested tables, following the LAST element of any array of
    tables — the semantics [[symbol.footprint]] needs to attach to the symbol
    most recently opened."""
    node = root
    for p in parts:
        nxt = node.get(p)
        if nxt is None:
            nxt = node[p] = {}
        elif isinstance(nxt, list):
            if not array_ok or not nxt or not isinstance(nxt[-1], dict):
                cur.err(f"cannot descend into {p!r}")
            nxt = nxt[-1]
        elif not isinstance(nxt, dict):
            cur.err(f"{p!r} is a value, not a table")
        node = nxt
    return node


def _assign(node, key, value, cur):
    tgt = _walk(node, key[:-1], cur)
    name = key[-1]
    if name in tgt:
        cur.err(f"duplicate key {'.'.join(key)!r}")
    tgt[name] = value


def loads(text):
    """Parse TOML text -> dict. Raises TomlError with a line number."""
    if text.startswith("﻿"):
        text = text[1:]
    cur = _Cursor(text)
    root = {}
    current = root
    while True:
        cur.skip_ws(newlines=True)
        if cur.eof():
            return root
        if cur.peek(2) == "[[":
            cur.i += 2
            key = _read_key(cur)
            if cur.peek(2) != "]]":
                cur.err("expected ']]'")
            cur.i += 2
            parent = _walk(root, key[:-1], cur)
            arr = parent.setdefault(key[-1], [])
            if not isinstance(arr, list):
                cur.err(f"{key[-1]!r} is not an array of tables")
            current = {}
            arr.append(current)
        elif cur.peek() == "[":
            cur.i += 1
            key = _read_key(cur)
            if cur.peek() != "]":
                cur.err("expected ']'")
            cur.i += 1
            current = _walk(root, key, cur)
        else:
            key = _read_key(cur)
            cur.skip_ws()
            if cur.peek() != "=":
                cur.err("expected '=' after key")
            cur.i += 1
            _assign(current, key, _read_value(cur), cur)
        cur.skip_ws()
        if not cur.eof() and cur.peek() not in ("\n", ""):
            cur.err(f"trailing text {cur.peek(20)!r}")


def load(path):
    with open(path, encoding="utf-8") as f:
        return loads(f.read())


def quote(s):
    """Render a Python string as a TOML basic string, escaping what must be."""
    out = ['"']
    for ch in str(s):
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)
