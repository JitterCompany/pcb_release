#!/usr/bin/env python3
"""Resolve, fetch and VERIFY datasheets.

The verification half is the point. A datasheet that is merely *present* proves
nothing -- this repo already contains two counterexamples:

  * datasheets/ads114s08b.pdf is the B variant (SBAS852A); the fitted part needs
    SBAS815A. A substring test passes it, because "ADS114S08" really is inside
    "ADS114S08B". Hence whole-token matching.
  * datasheets/quectel_ec21_series_hardware_design_v2-3-1.pdf is an EC21
    document sitting next to an EG915N BOM line.

A symbol's Datasheet field is a claim like any other and gets checked the same
way; when it resolves to the wrong part that is a defect in the SCHEMATIC, worth
fixing at source.

Fetching is deliberately plain HTTP/1.1 with an explicit `Connection: close`,
and a User-Agent that names bomcheck. The previous attempt at this failed on st.com HTTP/2
INTERNAL_ERROR and on 403s from vendor CDNs; measured from this host, TI now
succeeds and ST still times out, which is why a distributor mirror is a
first-class fallback rather than an afterthought.

Nothing here raises on failure. A datasheet we could not get is DATA -- it
becomes one batched line for a human, never a crashed run.
"""
import hashlib
import os
import re
import subprocess
import time
import urllib.error
import urllib.request

import atomic

# Says what it is. A site that refuses a script gets the person to download
# the file instead (the wizard asks for it); bomcheck does not pose as a browser.
UA = "bomcheck (+https://github.com/JitterCompany/pcb_release)"

# Vendor document ids: TI SCDS398C / SBAS815A, ST DS13096, Nexperia, Diodes...
_DOCID = re.compile(
    r"\b(?:S[A-Z]{3,4}\d{3,4}[A-Z]?|DS\d{4,5}|[A-Z]{2,4}\d{4,6}[A-Z]?)\b")

# How many characters of extracted text below which we call a PDF unreadable
# (scanned images yield almost nothing through pdftotext).
_MIN_TEXT = 400


class Fetched:
    __slots__ = ("mpn", "path", "url", "origin", "sha256", "pages", "text",
                 "error")

    def __init__(self, mpn, path=None, url="", origin="", sha256="", pages=0,
                 text="", error=""):
        self.mpn, self.path, self.url, self.origin = mpn, path, url, origin
        self.sha256, self.pages, self.text, self.error = sha256, pages, text, error

    @property
    def ok(self):
        return bool(self.path and not self.error)


def http_get(url, timeout=25, max_bytes=40_000_000):
    """-> (bytes, error_str). Never raises."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/pdf,application/octet-stream,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        # HTTP/1.1 with an explicit close: urllib speaks 1.1 only, which is
        # precisely why it survives the vendor CDNs that break HTTP/2 clients.
        "Connection": "close",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(max_bytes), ""
    except urllib.error.HTTPError as e:
        return b"", f"HTTP {e.code} {e.reason}"
    except Exception as e:                                       # noqa: BLE001
        return b"", f"{type(e).__name__}: {e}"


def pdf_to_text(path):
    """-> (text, pages). Uses poppler's pdftotext -layout, which preserves the
    column structure that pin tables live in. Returns ('', 0) on any failure."""
    try:
        out = subprocess.run(["pdftotext", "-layout", path, "-"],
                             capture_output=True, timeout=120)
        text = out.stdout.decode("utf-8", "replace")
    except Exception:                                            # noqa: BLE001
        return "", 0
    return text, text.count("\f") + (1 if text else 0)


def resolve(mpn, *, symbol_url="", local_dir="", approved=None, mirror_url=""):
    """Pick a source for this MPN's datasheet, best-provenance first.

    Order: an already-approved binding (a human said yes once, do not re-ask) ->
    a PDF archived in the project -> the symbol's own Datasheet field -> a
    distributor mirror. Each candidate is returned with its origin so a later
    verdict can say how much to trust it.
    """
    out = []
    rec = (approved or {}).get(mpn)
    if rec and rec.get("url"):
        out.append(("approved", rec["url"]))
    if local_dir and os.path.isdir(local_dir):
        # Most name-similar first. An archive of a dozen datasheets otherwise
        # gets fully parsed for every part, and -- worse -- a part whose own
        # file is alphabetically late is judged against unrelated documents
        # first, so a lucky family-only match on the wrong file can win.
        names = [n for n in os.listdir(local_dir) if n.lower().endswith(".pdf")]
        key = re.sub(r"[^a-z0-9]", "", mpn.lower())

        def similarity(name):
            base = re.sub(r"[^a-z0-9]", "", name.lower())
            for n in range(len(key), 3, -1):
                if key[:n] in base:
                    return -n                      # longer shared prefix first
            return 0

        for name in sorted(names, key=lambda n: (similarity(n), n)):
            out.append(("local-archive", os.path.join(local_dir, name)))
    if symbol_url:
        out.append(("symbol-field", symbol_url))
    if mirror_url:
        out.append(("mouser-mirror", mirror_url))
    out.append(("local-copy", local_key(mpn)))
    return out


def field_kind(value):
    """What a symbol's Datasheet field points at: url, file, embedded or none."""
    v = (value or "").strip()
    if not v or v == "~":
        return "none"
    if v.lower().startswith(("http://", "https://")):
        return "url"
    if v.startswith("kicad-embed://"):
        return "embedded"
    return "file"


def field_path(value, project):
    """A file-kind field as an absolute path. KiCad resolves ${KIPRJMOD} and
    relative paths against the project directory."""
    project = os.path.abspath(project)   # a relative one would be joined twice
    v = (value or "").strip().replace("${KIPRJMOD}", project)
    return v if os.path.isabs(v) else os.path.normpath(os.path.join(project, v))


def field_source(value, project):
    """-> something fetch() can read, or "" (embedded files are not readable yet)."""
    kind = field_kind(value)
    if kind == "url":
        return value.strip()
    if kind == "file":
        return field_path(value, project)
    return ""


def local_key(mpn):
    """Cache slot for a PDF that exists only on this machine: confidential,
    too large to commit, or embedded in a schematic we cannot unpack."""
    return f"bomcheck://local/{mpn}"


def default_cache():
    """Outside the repo so a fresh checkout does not download everything again."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.environ.get("BOMCHECK_CACHE") or os.path.join(base, "bomcheck",
                                                            "datasheets")


# A vendor that times out today will time out on every run; waiting 25 s per
# part each time is what made `review` take minutes.
RETRY_FAILED_AFTER_S = 24 * 3600


def _cache_stem(src, cache_dir):
    is_url = src.lower().startswith(("http://", "https://", "bomcheck://"))
    ident = src if is_url else f"{os.path.abspath(src)}:{os.path.getmtime(src)}"
    return os.path.join(cache_dir, hashlib.sha256(ident.encode()).hexdigest()[:20])


def add_local_copy(url, pdf, cache_dir):
    """Store a PDF someone downloaded by hand as what `url` serves.

    For documents behind a login (Quectel): the schematic keeps the page a
    person can log in at, and every later fetch of that URL finds this copy.
    Returns the error string, or "" on success.
    """
    with open(pdf, "rb") as f:
        data = f.read()
    if not data.startswith(b"%PDF-"):
        return f"{pdf} is not a PDF"
    os.makedirs(cache_dir, exist_ok=True)
    stem = _cache_stem(url, cache_dir)
    atomic.write_bytes(stem + ".pdf", data)
    text, _pages = pdf_to_text(stem + ".pdf")
    atomic.write_text(stem + ".txt", text)
    if os.path.isfile(stem + ".err"):
        os.remove(stem + ".err")
    return ""


def fetch(mpn, origin, src, cache_dir):
    """Get one candidate into the cache as a PDF + its text. Never raises.

    Keyed by the SOURCE, not the MPN: one part has several candidate documents
    and they must not overwrite each other.
    """
    os.makedirs(cache_dir, exist_ok=True)
    is_url = src.lower().startswith(("http://", "https://", "bomcheck://"))
    if not is_url and not os.path.isfile(src):
        return Fetched(mpn, origin=origin, url=src, error="no such file")
    stem = _cache_stem(src, cache_dir)
    url = src if is_url else ""

    if os.path.isfile(stem + ".pdf") and os.path.isfile(stem + ".txt"):
        text = open(stem + ".txt", encoding="utf-8").read()
        data = open(stem + ".pdf", "rb").read()
        return Fetched(mpn, path=stem + ".pdf", url=url, origin=origin,
                       sha256=hashlib.sha256(data).hexdigest(),
                       pages=text.count("\f") + (1 if text else 0), text=text)
    if os.path.isfile(stem + ".err"):
        age = time.time() - os.path.getmtime(stem + ".err")
        if age < RETRY_FAILED_AFTER_S:
            err = open(stem + ".err", encoding="utf-8").read()
            return Fetched(mpn, origin=origin, url=url,
                           error=f"{err} (cached failure, retried after 24 h)")

    if src.startswith("bomcheck://"):
        return Fetched(mpn, origin=origin, url=src,
                       error="no local copy on this machine (bomcheck add-pdf)")
    if is_url:
        data, err = http_get(src)
        if not err and not data.startswith(b"%PDF-"):
            head = data[:80].decode("latin-1", "replace").replace("\n", " ")
            err = f"not a PDF (starts {head!r})"
        if err:
            with open(stem + ".err", "w", encoding="utf-8") as f:
                f.write(err)
            return Fetched(mpn, origin=origin, url=url, error=err)
    else:
        data = open(src, "rb").read()

    atomic.write_bytes(stem + ".pdf", data)
    text, pages = pdf_to_text(stem + ".pdf")
    atomic.write_text(stem + ".txt", text)
    return Fetched(mpn, path=stem + ".pdf", url=url, origin=origin,
                   sha256=hashlib.sha256(data).hexdigest(), pages=pages, text=text)


def _tokens(text):
    """Uppercase alphanumeric-ish tokens, split so a part number stays whole.

    Split on everything EXCEPT [A-Z0-9] plus the characters that legitimately
    appear inside order codes (- / . +), so ADS114S08B is one token and cannot
    be mistaken for ADS114S08.
    """
    return set(re.findall(r"[A-Z0-9][A-Z0-9\-/.+]*", text.upper()))


def base_mpn(mpn):
    """Strip packing/ordering suffixes to get the family stem.

    Deliberately CONSERVATIVE. This stem is only ever used to weaken a verdict
    from wrong-part to family-only, so over-stripping would let a datasheet for a
    neighbouring part look acceptable -- and neighbouring parts are exactly what
    this tool exists to separate (TMUX1109 vs TMUX1309, SMA6J13A vs SMA6J13CA).
    When unsure, strip less.
    """
    m = mpn.upper().strip()
    # explicit packing suffix after a separator
    m = re.sub(r"[-/](?:TR|T&R|REEL|TAPE|CT|E[1-4]|T[1-3]|G|GE[1-4]|Q1|ND)$", "", m)
    # trailing reel/packing letter on a long code (TMUX1309APWR -> TMUX1309APW)
    if len(m) > 8:
        m = re.sub(r"(?<=[A-Z])(?:R|T)$", "", m)
    return m


# A part number is letters, then digits and letters mixed. Requiring TWO
# consecutive digits after the letters excludes a whole style of order code --
# TPD4E05U06 has a single "4" -- and the part then counts as absent from its own
# datasheet, which reads as "wrong document" and blocks a perfectly good one.
_PARTLIKE = re.compile(r"\b([A-Z]{2,6}[0-9][A-Z0-9]{2,14})\b")


def _common_prefix(a, b):
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return n


def subject_check(mpn, text, min_prefix=8, ratio=5, floor=20):
    """Is this document ABOUT our part, or does it merely mention it?

    -> (ok, why). Not a substitute for the whole-token test; an extra signal
    that catches a specific and expensive mistake.

    A schematic on this board links TXU0204PWR to txu0304.pdf. That document
    names TXU0304 seventy-five times and TXU0204 four times, in a
    related-products table -- so the exact-token test says "family-only" and
    extraction then reads TXU0304's pin map, giving pin 4 as A3 for a part whose
    pin 4 is A3Y. The part number is present; the document is simply about
    something else.

    So sibling part numbers are counted. A token is COMPATIBLE with our MPN if
    one starts with the other or they share a long prefix -- STM32L4R5xx and
    STM32L4R5VIT6 are the same device family and both belong to DS12023. A
    RIVAL that dominates a barely-mentioned part means we are reading the wrong
    document.
    """
    up = re.sub(r"[^A-Z0-9]", "", mpn.upper())
    counts = {}
    for m in _PARTLIKE.finditer(text.upper()):
        t = re.sub(r"[^A-Z0-9]", "", m.group(1))
        counts[t] = counts.get(t, 0) + 1

    ours = rivals = 0
    rival_name = ""
    for tok, n in counts.items():
        if not tok[:2].isalpha():
            continue
        pre = _common_prefix(tok, up)
        compatible = (up.startswith(tok) or tok.startswith(up)
                      or pre >= min_prefix)
        # Only a token from the same alphabetic family can be a rival; a
        # document number like DS12023 is not a competing part.
        same_family = pre >= 3
        if compatible:
            ours = max(ours, n)
        elif same_family and n > rivals:
            rivals, rival_name = n, tok

    if rivals >= ratio * max(ours, 1) and ours <= floor:
        return False, (f"this document is about {rival_name} ({rivals} "
                       f"mentions); {mpn} appears {ours} time(s), which reads "
                       f"as a cross-reference, not the subject")
    return True, ""


def verify(mpn, text, manufacturer=""):
    """Score a fetched datasheet against the MPN it is supposed to describe.

    -> dict(verdict, why, doc_ids, exact, base, pages_with_mpn)

    verdict:
      confirmed   the exact MPN appears as a WHOLE TOKEN
      family-only the base part number appears but the exact orderable does not
      wrong-part  neither appears, or a different manufacturer dominates
      unreadable  pdftotext produced essentially nothing (scanned PDF)
    """
    if len(text.strip()) < _MIN_TEXT:
        return {"verdict": "unreadable", "why": "pdftotext produced almost no text "
                "(likely a scanned/image PDF)", "doc_ids": [], "exact": False,
                "base": False, "manufacturer_seen": None, "pages_with_mpn": []}

    up = mpn.upper().strip()
    toks = _tokens(text)
    exact = up in toks
    stem = base_mpn(up)
    base = any(t == stem or t.startswith(stem) for t in toks)

    # A SERIES document covers the part but may never print a full order code:
    # Quectel's EG915N hardware design says "EG915N" 153 times and never lists
    # EG915NEAAC-N05-SNNSA.
    #
    # The family stem is found by SHORTENING the order code until a prefix
    # appears as a whole token -- a fixed pattern cannot do it, because order
    # codes are shaped differently per vendor: "^[A-Z]+[0-9]+" yields EG915 for
    # EG915NEAAC (one character short, matches nothing) and S25 for
    # S25FL064LABMFI013 (far too short to mean anything).
    #
    # WHOLE TOKEN is the load-bearing part, and the difference between this and
    # the ADS114S08 trap: the B-variant document contains only "ADS114S08B"
    # tokens, so no prefix of ADS114S08IPBSR ever stands alone in it and the
    # document stays rejected.
    # A stem must be DISTINCTIVE: letters and digits together. Shortening
    # without that rule reaches "1500" for Wuerth's 150060VS75000, and "1500"
    # occurs in any document with a number in it -- a Grundfos pump manual in
    # this project's own datasheet folder verified as a "series document" for a
    # terminal block on exactly that basis.
    root, root_whole = "", False
    for n in range(len(up), 3, -1):
        cand = up[:n].rstrip("-_/")
        if len(cand) < 4 or cand not in toks:
            continue
        if not (any(ch.isalpha() for ch in cand)
                and any(ch.isdigit() for ch in cand)):
            continue
        root, root_whole = cand, True
        break

    pages = []
    for i, page in enumerate(text.split("\f"), 1):
        if up in _tokens(page):
            pages.append(i)
            if len(pages) >= 6:
                break

    doc_ids = []
    head = text[:6000].upper()
    for m in _DOCID.finditer(head):
        d = m.group(0)
        if d not in doc_ids and not d.startswith(stem[:4]):
            doc_ids.append(d)
        if len(doc_ids) >= 4:
            break

    # Manufacturer is CORROBORATING ONLY and can never demote an exact match.
    # A whole-token hit on the full order code is far stronger evidence than a
    # vendor name, and vendor names are unreliable in text: TI's letterhead is a
    # logo image, so "Texas Instruments" first appears ~7 kB into its own
    # datasheet. Searched over the whole document, and reported, not gated.
    mfr_seen = None
    if manufacturer:
        key = re.sub(r"[^A-Z]", "", manufacturer.upper())[:6]
        mfr_seen = bool(key) and key in re.sub(r"[^A-Z]", "", text.upper())

    subject_ok, subject_why = subject_check(mpn, text)
    if not subject_ok:
        return {"verdict": "wrong-part", "why": subject_why, "doc_ids": doc_ids,
                "exact": exact, "base": base, "manufacturer_seen": None,
                "pages_with_mpn": pages}

    if exact:
        note = ""
        if mfr_seen is False:
            note = f"; note: {manufacturer!r} is never named in the text"
        return {"verdict": "confirmed",
                "why": f"{mpn} appears as a whole token on page(s) "
                       f"{', '.join(map(str, pages)) or '?'}{note}",
                "doc_ids": doc_ids, "exact": True, "base": base,
                "manufacturer_seen": mfr_seen, "pages_with_mpn": pages}
    if base:
        return {"verdict": "family-only",
                "why": f"base {stem!r} appears but the exact orderable {mpn!r} "
                       f"does not -- confirm this document covers the fitted suffix",
                "doc_ids": doc_ids, "exact": False, "base": True,
                "manufacturer_seen": mfr_seen, "pages_with_mpn": []}
    if root_whole:
        return {"verdict": "family-only",
                "why": f"a series document for {root!r}; it never prints the full "
                       f"order code {mpn!r} -- confirm it covers this variant",
                "doc_ids": doc_ids, "exact": False, "base": False,
                "manufacturer_seen": mfr_seen, "pages_with_mpn": []}
    return {"verdict": "wrong-part",
            "why": f"neither {mpn!r} nor base {stem!r} appears anywhere in the text",
            "doc_ids": doc_ids, "exact": False, "base": False,
            "manufacturer_seen": mfr_seen, "pages_with_mpn": []}


# ---------------------------------------------------------------------------
# Guessing a datasheet URL, safely
# ---------------------------------------------------------------------------
#
# 12 of the 26 pin-mapped MPNs on the first test board carry no Datasheet field at all, and
# there is no distributor API in this workflow by design. Several vendors do
# publish at a predictable path, so the URL can be GUESSED -- which is only
# acceptable because verify() is the judge afterwards. A guess that lands on the
# wrong part is rejected exactly like a wrong archived PDF, so the failure mode
# is "no datasheet", never "the wrong datasheet".
#
# These are fetch HINTS, not authoritative vendor data. Nothing downstream trusts
# them; they only decide what to try.

def _ti_stems(mpn):
    """TI order codes append package+packing to a family stem, irregularly:
    ADS114S08IPBSR -> ads114s08, TXU0204PWR -> txu0204, TPS7A2033PDBVR ->
    tps7a20. So several stems are offered longest-first and the first one that
    VERIFIES wins."""
    m = re.sub(r"[^A-Z0-9]", "", mpn.upper())
    out = []
    for cut in range(len(m), 3, -1):
        stem = m[:cut].lower()
        if stem not in out:
            out.append(stem)
    return out[:14]


def candidate_urls(mpn, manufacturer=""):
    """-> [(origin, url)] worth trying for a part with no recorded datasheet."""
    mfr = re.sub(r"[^a-z]", "", (manufacturer or "").lower())
    up = mpn.upper()
    out = []
    ti = ("texasinstruments" in mfr
          or re.match(r"^(ADS|DAC|TMUX|TPS|TPD|TXU|TXS|SN74|LM|OPA|INA|UCC|"
                      r"LMK|TLV|TCA|CSD|BQ|MSP)", up))
    if ti:
        for stem in _ti_stems(mpn):
            out.append(("ti-pattern",
                        f"https://www.ti.com/lit/ds/symlink/{stem}.pdf"))

    # Wuerth: the part number IS the filename. Both paths are live and both
    # appear in this project's own schematic, so both are tried.
    if "wurth" in mfr or "wrth" in mfr or re.match(r"^(69\d|74\d|150\d)", up):
        pn = re.sub(r"[^A-Za-z0-9]", "", up)
        for path in ("components/products/datasheet", "catalog/datasheet"):
            out.append(("we-pattern", f"https://www.we-online.com/{path}/{pn}.pdf"))

    # Diodes Inc: base part without the packing suffix (-7, -13, ...).
    if "diodes" in mfr or re.match(r"^(DM[NP]|BAT|BAS|BZ|AP|ZX)", up):
        base = re.sub(r"-\d+$", "", up)
        out.append(("diodes-pattern",
                    f"https://www.diodes.com/datasheet/download/{base}.pdf"))
        out.append(("diodes-pattern",
                    f"https://www.diodes.com/assets/Datasheets/{base}.pdf"))

    # Renesas / Dialog: a slug, not a PDF path.
    if "renesas" in mfr or "dialog" in mfr or re.match(r"^SLG", up):
        out.append(("renesas-pattern",
                    f"https://www.renesas.com/us/en/document/dst/"
                    f"{up.lower()}-datasheet"))
    return out
