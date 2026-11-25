#!/usr/bin/env python3
"""Walk the to-do list one BOM line at a time, asking only what needs a person.

Every answer is written the moment it is given: a pin name into the table, a
URL into decisions-fields.csv, a signature into the table header. So quitting
halfway loses nothing, and a spreadsheet or a text editor stays an equal way
to make the same edits.

What the wizard never does is show the symbol's pin names while you type a
table. A name copied from the symbol would make the table agree with the thing
it exists to check.
"""
import datetime
import os
import re
import shutil
import signal
import subprocess
import textwrap
import urllib.parse

import atomic
import library as LIB
import datasheet as D
import decisions as DC
import exempt as EX
import footprint as FP
import interface as IF
import netlist as N
import pintable as P
import polarity as PO
import reffile as R
import symgraph as SG
import todo as TD
import tomlite
import tui
import waivers as WV


class Quit(Exception):
    pass


class Back(Exception):
    """Undo this part's answers and ask it again; with none yet, the part
    before."""


class Skip(Exception):
    pass


numbered_only = TD.numbered_only


class Wizard:
    def __init__(self, att, project, lines, items, ask=input, say=print,
                 cache=None, today=None, auto_open=None, rev="", by="", ui=None):
        self.att, self.project = att, project
        self.lines = {ln.mpn: ln for ln in lines}
        self.items = items
        self.recollect = None   # -> (lines, items) as collect() gives them, fresh
        self.library = LIB.component_library(project) if project else ""
        self.save_to = "ask"    # ask | library | project
        self.saved_in_library = []
        self.stopped = False
        self.ask_raw, self.say = ask, say
        # Another front end (webui.WebUI): it draws the questions as buttons
        # and shows the datasheet and footprint itself. The questions stay here.
        self.ui = ui
        if ui is not None:
            self.say = ui.say
        self.cache = cache or D.default_cache()
        self.today = today or datetime.date.today().isoformat()
        self.review = os.path.join(att, "review")
        self.fields_csv = os.path.join(self.review, "decisions-fields.csv")
        self.by = by
        self.changed_fields = 0
        self.line = None
        self.rev = rev
        # Open the datasheet by itself when a step needs reading it, once per
        # line. Off without a display, or with --no-open.
        has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        self.auto_open = has_display if auto_open is None else auto_open
        self.opened = set()
        self.viewers = []   # windows this run opened, closed after each part
        self.session_pdf = {}
        self.tried_url = {}   # mpn -> a URL dropped in the browser that would not download
        self.part_no, self.answered = 0, 0   # for "back": where we are
        self.tui = ask is input and ui is None and tui.usable()
        self.buttons = self.tui or ui is not None

    # ---- prompting --------------------------------------------------------
    def ask(self, prompt, choices=None, default="", labels=None, files=False,
            extra=()):
        """One answer. Commands start with a colon so no typed answer can
        collide with one (a FET has a pin called S): :o opens the datasheet,
        :s skips the line, :q quits. In a multiple-choice prompt the bare
        letters work too. Ctrl-C and Ctrl-D quit anywhere. On a terminal the
        choices are buttons (tui.py), and :o :s :q still work when typed.

        In the browser a PDF or URL dropped on the page arrives as ":drop X".
        Where the prompt asks for a file or URL (`files`), X is the answer;
        anywhere else it becomes this part's datasheet and the prompt repeats."""
        while True:
            hint = f" [{'/'.join(choices)}]" if choices else ""
            hint += f" ({default})" if default else ""
            try:
                if self.ui is not None:
                    buttons, actions = self.buttons_for(choices, labels or {},
                                                        extra)
                    ans = self.ui.ask(prompt.strip(), buttons, default,
                                      not choices, actions).strip()
                elif self.tui:
                    ans = self.ask_tui(prompt, choices, default, labels or {},
                                       extra)
                else:
                    ans = self.ask_raw(f"{prompt}{hint}: ").strip()
            except (EOFError, KeyboardInterrupt, tui.Stop):
                raise Quit()
            # In a multiple-choice prompt a bare letter cannot be an answer
            # unless it is listed, so q/s/o work there without the colon.
            if choices and ans in ("q", "s", "o", "b") and ans not in choices:
                ans = ":" + ans
            if ans == ":q":
                raise Quit()
            if ans == ":b" and self.can_back():
                raise Back()
            if ans == ":s":
                raise Skip()
            if ans == ":o" and self.line is not None:
                self.show_pdf(self.line, force=True)
                continue
            if ans.startswith(":drop "):
                if files:
                    self.answered += 1
                    return ans[6:]
                self.dropped(ans[6:])
                continue
            ans = ans or default
            if not choices or ans in choices:
                self.answered += 1
                return ans
            self.say(f"  answer one of {', '.join(choices)} "
                     f"(or s to skip, q to stop)")

    def buttons_for(self, choices, labels, extra=()):
        """-> [(value, label)] for the answers, and for this prompt's own
        commands (`extra`, e.g. :n) and datasheet/back/skip/stop. Every
        command a terminal user can type is a button too."""
        words = {"y": "yes", "n": "no"}
        buttons = [(c, labels.get(c) or words.get(c, c)) for c in choices or []]
        actions = list(extra) + ([(":o", "datasheet")] if self.line is not None else []) + \
            ([(":b", "back")] if self.can_back() else []) + \
            [(":s", "skip part"), (":q", "stop")]
        return buttons, actions

    def can_back(self):
        return self.part_no > 1 or (self.part_no == 1 and self.answered > 0)

    def ask_tui(self, prompt, choices, default, labels, extra=()):
        buttons, actions = self.buttons_for(choices, labels, extra)
        indent = len(prompt) - len(prompt.lstrip())
        return tui.prompt(prompt.strip(), buttons, default, text=not choices,
                          actions=actions, indent=indent).strip()

    def legend(self, text):
        """What each letter means; where there are buttons they say it."""
        if not self.buttons:
            self.say(text)

    def close_viewers(self):
        """Close the windows opened for this part. A viewer that handed the
        file to a window already open has exited, and that window stays."""
        for proc in filter(None, self.viewers):
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except OSError:
                    pass
        self.viewers = []
        if self.ui is not None:
            self.ui.end_part()

    def tell(self, text, indent=4):
        """Say a sentence, wrapped, never cut off."""
        if self.ui is not None:   # it wraps to its own width
            return self.say(" " * indent + text)
        for line in textwrap.wrap(text, 88 - indent) or [""]:
            self.say(" " * indent + line)

    def rel(self, p):
        return os.path.relpath(p, os.path.dirname(self.att))

    # ---- entry ------------------------------------------------------------
    def run(self):
        lines = self.order()
        if not lines:
            self.say("Nothing needs you. `bomcheck` to see the state.")
            return 0
        if self.ui is not None:
            self.say(f"{len(lines)} part(s) need you. Every answer is saved as "
                     f"you go.")
        elif self.tui:
            self.say(f"{len(lines)} part(s) need you. Every answer is saved as "
                     f"you go. Tab or the arrow keys move, Enter picks, Esc stops. "
                     f"Typing :o :s :q also opens the datasheet, skips, stops.")
        else:
            self.say(f"{len(lines)} part(s) need you. Every answer is saved as "
                     f"you go. o = open the datasheet, s = skip this part, q = "
                     f"stop (Ctrl-C works too); where you type a name or URL, "
                     f"use :o :s :q.")
        try:
            # The same name as on the commits that carry these tables, so
            # git history and reviewed_by say the same who.
            self.by = (self.by or _git_user(self.project)
                       or (_remembered_name() if self.ask_raw is input else ""))
            if self.by:
                self.say(f"Checking as {self.by} (bomcheck wizard --by NAME "
                         f"to change).")
            else:
                self.by = self.ask("Your name, recorded with what you check",
                                   default=_git_user(self.project))
            if self.ask_raw is input:
                _remember_name(self.by)
            skipped = set()
            while lines:
                self.one_pass(lines, skipped)
                # An answer can leave something open (a retyped name that
                # still differs). Check again, and ask again, but never
                # about a part skipped this session.
                lines = [ln for ln in self.reorder() if ln[0] not in skipped]
                if lines:
                    self.say(f"\n{len(lines)} part(s) still have something open "
                             f"after your answers. Asking about those again.")
        except (Quit, KeyboardInterrupt):
            self.say("\nStopped. Your answers so far are saved; run the wizard "
                     "again to continue.")
            self.stopped = True
        finally:
            self.close_viewers()
        if self.changed_fields:
            self.say(f"\n{self.changed_fields} BOM field value(s) are in "
                     f"{self.rel(self.fields_csv)}.")
            self.say("Write them into the schematic with: bomcheck apply. Then "
                     "update the layout too: Update PCB from Schematic (F8).")
        new = LIB.uncommitted(self.library) if self.library else []
        if new:
            self.say(f"\n{len(new)} signed table(s) in {self.library} are not committed "
                     f"there yet. Commit them in that repository (a PR), so other "
                     f"boards get them.")
        return 0

    def reorder(self):
        if self.recollect is None:
            return []
        lines, self.items = self.recollect()
        self.lines = {ln.mpn: ln for ln in lines}
        return self.order()

    def summary(self):
        """What is left, for the finish page. -> {open: [...], fields: n}"""
        if self.recollect is not None:
            self.reorder()
        return {"open": [{"mpn": m, "refs": " ".join(its[0].refs),
                          "what": dict(TD.KINDS).get(its[0].kind, its[0].kind)}
                         for m, its in self.order()],
                "fields": len(DC.parse_fields(self.fields_csv)),
                "library": os.path.basename(self.library) if self.library else "",
                "library_new": len(LIB.uncommitted(self.library)) if self.library else 0,
                "stopped": self.stopped}

    def one_pass(self, lines, skipped):
        before = []   # the answer files before each part, for "back"
        n = 1
        while n <= len(lines):
            mpn, its = lines[n - 1]
            if len(before) < n:
                before.append(self.snapshot())
            self.part_no, self.answered = n, 0
            ln = self.lines.get(mpn)
            self.line = ln
            refs = " ".join(ln.refs if ln else its[0].refs)
            maker = ln.comp.field("Manufacturer").strip() if ln else ""
            about = (f"{ln.comp.value} | " + (f"{maker} | " if maker else "")
                     + f"symbol {ln.comp.libsource} | "
                     f"footprint {ln.comp.footprint}") if ln else ""
            if self.ui is not None:
                detail = (f"value {ln.comp.value} | symbol {ln.comp.libsource}"
                          f" | footprint {ln.comp.footprint}") if ln else ""
                self.ui.part(n, len(lines), mpn or "(no MPN)", refs, detail,
                             maker)
                if ln:   # the pane has room: show it for every question
                    self.show_pdf(ln)
            else:
                self.say(f"\n=== {n}/{len(lines)}  {mpn or '(no MPN)'}  {refs}")
                if about:
                    self.say(f"    {about}")
            heads = dict(TD.KINDS)
            try:
                for it in its:
                    self.say(f"\n  {heads[it.kind]}")
                    getattr(self, "do_" + it.kind)(it, ln)
            except Skip:
                skipped.add(mpn)
                self.say("    skipped")
            except Back:
                n = n if self.answered else n - 1
                self.restore(before[n - 1])
                del before[n:]
                self.close_viewers()
                self.say(f"\n  Back: the answers for part {n} are undone. "
                         f"Asking them again.")
                continue
            self.close_viewers()
            n += 1

    def order(self):
        """Group items by line; a line's place is set by its first kind."""
        rank = {k: i for i, (k, _h) in enumerate(TD.KINDS)}
        groups = {}
        for it in self.items:
            if it.kind in TD.AUTOMATIC or it.kind == "apply_pending":
                continue
            key = it.mpn or " ".join(it.refs)
            groups.setdefault(key, []).append(it)
        for its in groups.values():
            its.sort(key=lambda i: rank[i.kind])
        return sorted(groups.items(), key=lambda kv: (rank[kv[1][0].kind], kv[0]))

    def snapshot(self):
        """Every answer file as it is now. Answers are only written under
        attestations/, and it is small (some 100 kB)."""
        files = {}
        for root, _dirs, names in os.walk(self.att):
            for name in names:
                p = os.path.join(root, name)
                with open(p, "rb") as f:
                    files[p] = f.read()
        return files, self.changed_fields, dict(self.session_pdf), dict(self.tried_url)

    def restore(self, snap):
        files, self.changed_fields, session_pdf, tried_url = snap
        self.session_pdf, self.tried_url = dict(session_pdf), dict(tried_url)
        for root, _dirs, names in os.walk(self.att):
            for name in names:
                p = os.path.join(root, name)
                if p not in files:
                    os.remove(p)
        for p, data in files.items():
            try:
                with open(p, "rb") as f:
                    if f.read() == data:
                        continue
            except OSError:
                pass
            os.makedirs(os.path.dirname(p), exist_ok=True)
            atomic.write_bytes(p, data)

    # ---- BOM fields -------------------------------------------------------
    def do_mpn_missing(self, it, ln):
        mpn = self.ask(f"    MPN for {' '.join(it.refs)}")
        if mpn:
            DC.set_field(self.fields_csv, f"({it.refs[0]})", it.refs, "MPN", mpn)
            self.changed_fields += 1

    def do_datasheet_missing(self, it, ln):
        if it.detail:
            self.say(f"    {it.detail}")
        # The PDF the pane already shows comes first: asking for a datasheet
        # next to one on screen makes no sense.
        shown = ln and self.known_pdf(ln)
        guess = shown or _guess_pdf(it.mpn, self.project, self.cache)
        if guess:   # relative to where bomcheck runs, not to the project
            guess = os.path.abspath(guess)
        if guess and self.buttons:
            ans = self.confirm_guess(it, ln, os.path.abspath(guess))
            if ans in ("y", "x"):
                return None if ans == "y" else self.exempt(it.mpn)
            guess = ""
        if self.ui is not None:
            self.tell("Drop the PDF or its URL on this page, or paste a URL (a "
                      "login page is fine) or a path. n = there is none.")
        else:
            self.tell("Paste a URL (a login page is fine) or a PDF path; n = there "
                      "is none" + ("; Enter = the file suggested." if guess else "."))
        while True:
            ans = self.ask("    datasheet", default=self.rel(guess) if guess else "",
                           files=True)
            if ans == "n":
                self.say("    Then its pins are typed by hand, with nothing to "
                         "check them against. Why is there none?")
                return self.exempt(it.mpn)
            if ans.startswith("http"):
                if self.datasheet_url(it, ln, ans):
                    return
                continue
            path = self.abspath(ans)
            if path and os.path.isfile(path):
                if self.datasheet_file(it, ln, path):
                    return
                continue
            self.say("    not a URL, and no such file")

    def confirm_guess(self, it, ln, path):
        """Show the PDF bomcheck found and ask if it is the right one.
        -> y (taken), n (give another) or x (there is none)."""
        text, _pages = D.pdf_to_text(path)
        v = D.verify(it.mpn, text, ln.comp.field("Manufacturer") if ln else "")
        if v["verdict"] == "wrong-part":
            return "n"
        page = (v["pages_with_mpn"] or [1])[0]
        self.tell(f"bomcheck found {self.rel(path)}" + (
            f". It names {it.mpn} on page {page}." if v["verdict"] == "confirmed"
            else f", which does not name {it.mpn} exactly."))
        if self.ui is not None:
            self.ui.show_pdf(path, page)
        else:
            self.say(f"    datasheet: file://{urllib.parse.quote(path)}#page={page}")
        ans = self.ask(f"    Is this the datasheet for {it.mpn}", ["y", "n", "x"],
                       "y", labels={"y": "yes", "n": "no, give another",
                                    "x": "there is none"})
        if ans == "y" and not self.datasheet_file(it, ln, path, checked=True):
            return "n"
        return ans

    def abspath(self, ans):
        """A typed path: ~ expanded, relative ones as the prompt showed them."""
        if not ans:
            return ""
        if ans.startswith("file://"):   # a PDF's address in the browser
            ans = urllib.parse.unquote(urllib.parse.urlsplit(ans).path)
        p = os.path.expanduser(ans)
        return p if os.path.isabs(p) else os.path.join(os.path.dirname(self.att), p)

    def datasheet_url(self, it, ln, url):
        f = D.fetch(it.mpn, "wizard", url, self.cache)
        if not f.ok:
            self.tell(f"No PDF from that URL ({f.error}).")
            if not self.local_copy(it.mpn, url, ln):
                return False
        elif not self.accept_pdf(it.mpn, ln, f.text):
            return False
        else:
            self.session_pdf[it.mpn] = f.path
        self.set_datasheet(it, url)
        return True

    def datasheet_file(self, it, ln, path, checked=False):
        if not checked:
            text, _pages = D.pdf_to_text(path)
            if not self.accept_pdf(it.mpn, ln, text):
                return False
        self.session_pdf[it.mpn] = path
        project = os.path.abspath(self.project)
        # Inside the project, or elsewhere in its repo: either way a path
        # relative to the project survives any checkout.
        inside = _repo_root(project) or project
        if os.path.abspath(path).startswith(inside + os.sep):
            # Relative to the project, so the link survives any checkout.
            self.set_datasheet(it, "${KIPRJMOD}/" + os.path.relpath(path, project))
            return True
        self.say("    That file is outside the repository.")
        self.say("      c = copy it to the project's datasheets/ folder and link it")
        self.say("      x = keep it out of the repo (confidential, too large) "
                 "and use it on this machine only")
        if self.ask("    The file", ["c", "x"],
                    labels={"c": "copy into project", "x": "this machine only"}) == "c":
            dst_dir = os.path.join(project, "datasheets")
            dst = os.path.join(dst_dir, os.path.basename(path))
            if os.path.exists(dst) and not _same_file(path, dst):
                self.say(f"    {self.rel(dst)} exists and differs; rename one first")
                return False
            os.makedirs(dst_dir, exist_ok=True)
            shutil.copyfile(path, dst)
            self.say(f"    copied to {self.rel(dst)}")
            self.set_datasheet(it, "${KIPRJMOD}/datasheets/" + os.path.basename(path))
            return True
        D.add_local_copy(D.local_key(it.mpn), path, self.cache)
        self.exempt(it.mpn, ["confidential", "too_large", "other"])
        return True

    def accept_pdf(self, mpn, ln, text):
        """Say what the document is about; refuse the wrong part unless told."""
        v = D.verify(mpn, text, ln.comp.field("Manufacturer"))
        quoted = re.findall(r"'([^']+)'", v["why"])
        if v["verdict"] == "confirmed":
            pages = ", ".join(map(str, v["pages_with_mpn"])) or "?"
            self.tell(f"OK, this is the datasheet for {mpn} (named on page {pages}).")
        elif v["verdict"] == "family-only":
            family = quoted[0] if quoted else "the part family"
            self.tell(f"This covers the {family} series but never prints the order "
                      f"code {mpn}. Fine if that variant is in it.")
        elif v["verdict"] == "unreadable":
            self.tell("This PDF has no text to read (a scan?), so bomcheck cannot "
                      "tell which part it describes.")
        else:
            self.tell(f"This does not look like the datasheet for {mpn}: {v['why']}.")
        return v["verdict"] != "wrong-part" or \
            self.ask("    use it anyway", ["y", "n"], "n") == "y"

    def set_datasheet(self, it, value):
        DC.set_field(self.fields_csv, it.mpn, it.refs, "Datasheet", value)
        self.changed_fields += 1
        self.tell(f"Saved. `bomcheck apply` puts Datasheet = {value} in the "
                  f"schematic.")

    def datasheet_link(self, ln):
        """The URL a person can open for this part, or "". A URL dropped in
        the browser this session wins over the schematic's."""
        field = ln.comp.field("Datasheet")
        return self.tried_url.get(ln.mpn) or (
            field.strip() if D.field_kind(field) == "url" else "")

    def file_dropped_pdf(self, ln, path):
        """Store a PDF the person chose so later runs find it: under the
        schematic's URL, a URL they dropped, and the part number while the
        schematic has no URL. -> the cached copy, or "" after saying why."""
        field = ln.comp.field("Datasheet")
        keys = {self.tried_url.get(ln.mpn, "")}
        keys.add(field.strip() if D.field_kind(field) == "url"
                 else D.local_key(ln.mpn))
        keys.discard("")
        for k in sorted(keys):
            err = D.add_local_copy(k, path, self.cache)
            if err:
                self.tell(err)
                return ""
        return D.fetch(ln.mpn, "dropped", sorted(keys)[0], self.cache).path or path

    def dropped(self, what):
        """A PDF file or a URL dropped on the browser page, for this part."""
        ln = self.line
        if ln is None:
            return self.tell("Drop it while a part is shown.")
        if what.lower().startswith(("http://", "https://")):
            self.tried_url[ln.mpn] = what
            field = ln.comp.field("Datasheet").strip()
            # Dropped while a datasheet is shown: a better or newer one,
            # so it replaces the schematic's link.
            if D.field_kind(field) == "none" or (
                    D.field_kind(field) == "url" and field != what):
                DC.set_field(self.fields_csv, ln.mpn, ln.refs, "Datasheet", what,
                             current=field if D.field_kind(field) == "url" else "")
                self.changed_fields += 1
                self.tell(f"Saved. `bomcheck apply` puts Datasheet = {what} in "
                          f"the schematic.")
            f = D.fetch(ln.mpn, "dropped", what, self.cache)
            if not f.ok:
                self.tell(f"bomcheck cannot download it ({f.error}). Open it in "
                          f"your browser, save the PDF and drop the file here.")
                self.session_pdf.pop(ln.mpn, None)
                return self.ui.show_missing(what)
            what = f.path
        text, _pages = D.pdf_to_text(what)
        if not self.accept_pdf(ln.mpn, ln, text):
            return
        path = self.file_dropped_pdf(ln, what)
        if path:
            self.session_pdf[ln.mpn] = path
            field = ln.comp.field("Datasheet").strip()
            self.tell("Saved as this part's datasheet." + (
                f" The schematic still links {field}; drop the new URL too to "
                f"change that." if D.field_kind(field) == "url"
                and ln.mpn not in self.tried_url else ""))
            self.show_pdf(ln, force=True)

    def exempt(self, mpn, reasons=None):
        reasons = reasons or list(EX.REASONS)
        for r in reasons:
            self.say(f"      {r:13s} {EX.REASONS[r]}")
        reason = self.ask("    reason", reasons, labels={r: r.replace("_", " ")
                                                          for r in reasons})
        note = self.ask("    note (optional)")
        EX.add(self.att, mpn, reason, note, self.by, self.today)
        self.say(f"    recorded in {self.rel(EX.path_of(self.att))}")

    def pick_pdf(self, mpn, ln):
        """Ask for a PDF and check it is about this part. -> path or ""."""
        guess = _guess_pdf(mpn, self.project, self.cache)
        ans = self.ask("    PDF file", default=self.rel(guess) if guess else "",
                       files=True)
        if ans.lower().startswith(("http://", "https://")):
            f = D.fetch(mpn, "wizard", ans, self.cache)
            if not f.ok:
                self.tell(f"bomcheck cannot download that one either ({f.error}). "
                          f"Open it in your browser, save the PDF and give the "
                          f"file.")
                return ""
            return f.path if self.accept_pdf(mpn, ln, f.text) else ""
        path = self.abspath(ans)
        if not path:
            return ""
        if not os.path.isfile(path):
            self.say(f"    no such file: {path}")
            return ""
        text, _pages = D.pdf_to_text(path)
        return path if self.accept_pdf(mpn, ln, text) else ""

    def local_copy(self, mpn, url, ln):
        """Take a PDF downloaded by hand and file it under `url`."""
        self.say("    If you downloaded it yourself (a login, say), give the file; "
                 "it is used whenever this URL is looked up. Enter = other URL.")
        path = self.pick_pdf(mpn, ln)
        if not path:
            return False
        err = D.add_local_copy(url, path, self.cache)
        if err:
            self.say(f"    {err}")
        else:
            self.session_pdf[mpn] = path
        return not err

    # ---- tables -----------------------------------------------------------
    def do_stub_unfilled(self, it, ln):
        c = ln.comp
        hints = self.show_hints(it.path)
        self.show_pdf(ln, hints)
        if numbered_only(c.pins):
            self.say("    The symbol names none of these pins.")
            if self.ask("    Does the datasheet only number them too (no pin "
                        "functions)", ["y", "n"]) == "y":
                return self.numbers_only(it, ln)
        elif _connector_like(ln):
            self.tell("The symbol names its pins. A plain connector's datasheet "
                      "often only numbers its contacts; the names then come "
                      "from an interface spec, which is checked separately.")
            if self.ask("    Does the datasheet name these pins, or only number "
                        "them", ["n", "u"], "n",
                        labels={"n": "it names them",
                                "u": "it only numbers them"}) == "u":
                return self.numbers_only(it, ln)
        pictured = self.show_pads(ln)
        self.tell("Type each pin's name as the DATASHEET prints it (- for no "
                  "connect, empty to leave the pin for later"
                  + ("" if numbered_only(c.pins) else
                     ", :n if the datasheet only numbers the pins") + ")."
                  + (" The picture is the footprint on the board, "
                                 "with its pad numbers: pin N is pad N. Where "
                                 "the drawing only numbers its contacts (C1, "
                                 "C2 on a SIM holder), type those."
                                 if pictured else ""))
        var = _variant(it.path, ln.mpn)
        pin_cmds = [("-", "not connected")] + (
            [] if numbered_only(c.pins) else [(":n", "numbers only")])
        left = []
        for p in sorted(c.pins, key=lambda p: P._pinsort(p.num)):
            if var.pins.get(p.num):
                continue  # typed in an earlier session
            name = self.ask(f"    pin {p.num}" + (f" (pad {p.num})" if pictured
                                                    else ""), extra=pin_cmds)
            if name == ":n":
                return self.numbers_only(it, ln)
            if not name:
                left.append(p.num)
                continue
            R.set_pin_names(it.path, {p.num: "NC" if name == "-" else name})
        if left:
            self.say(f"    Left empty: pin {', '.join(left)}. The table stays a "
                     "draft; the next run asks for those pins again.")
            return
        self.check_table(it.path, ln)
        self.sign_and_promote(it.path, ln)

    def numbers_only(self, it, ln):
        """The datasheet only numbers the pins: the table claims the
        numbering, and the questions about the part's inside and pin 1 check
        the footprint. A symbol's names are NOT taken over: an interface
        table checks those, or nothing does and that stays open."""
        c = ln.comp
        R.make_numbered(it.path, [p.num for p in c.pins])
        if not numbered_only(c.pins):
            iface = TD.interface_of(c)
            self.tell("The part's table records the pin numbers only. It does "
                      "not check the symbol's pin names: "
                      + (f"the {iface} interface table does that." if iface else
                         f"an interface table does that, once the symbol has "
                         f"the field {TD.FIELD} = <name>."))
        if TD.asks_connections(_variant(it.path, ln.mpn), c.pins):
            self.ask_connections(it.path, ln)
        path = self.sign_and_promote(it.path, ln)
        return self.connection_result(path, ln)

    # ---- inside a numbered-only part ----------------------------------------
    def do_connections_unfilled(self, it, ln):
        self.ask_connections(it.path, ln)
        R.sign(it.path, self.by, self.today)
        self.connection_result(it.path, ln)

    def ask_connections(self, path, ln):
        """Which pins are connected inside the part, its dots, and whether
        the footprint is numbered like the datasheet's drawing."""
        pins = {p.num for p in ln.comp.pins}
        self.show_pdf(ln)
        self.tell("The datasheet only numbers the pins, so there are no names "
                  "to check. Its drawing still tells which pins are connected "
                  "inside the part (a winding, a switch contact), and where "
                  "pin 1 is.")
        while True:
            ans = self.ask("    Pins connected inside the part, per the datasheet "
                           "(e.g. 1-4 2-3; Enter = none)")
            groups = [g.split("-") for g in re.split(r"[\s,;]+", ans) if g]
            wrong = sorted({p for g in groups for p in g if p not in pins})
            if not wrong and all(len(g) > 1 for g in groups):
                break
            self.tell("Write each connection as pins joined by -, e.g. 1-4."
                      + (f" Not a pin of this part: {', '.join(wrong)}."
                         if wrong else ""))
        text = R.set_variant_key(open(path, encoding="utf-8").read(), "joined",
                                 groups)
        if len(groups) >= 2:   # coupled windings: the dots set the phase
            joined = {p for g in groups for p in g}
            while True:
                ans = self.ask("    Pins with a dot (start of a winding), per "
                               "the datasheet (e.g. 1 2; Enter = no dots)")
                dots = [p for p in re.split(r"[\s,;]+", ans) if p]
                if all(p in joined for p in dots):
                    break
                self.tell("Give pins that are part of a winding, e.g. 1 2.")
            text = R.set_variant_key(text, "dots", dots)
        pictured = self.show_pads(ln)
        self.tell(("The picture is the footprint on the board with its pad "
                   "numbers. " if pictured else "") +
                  "Compare it with the datasheet's drawing (top view): is pin 1 "
                  "in the same place, and do the numbers go round the same way?")
        same = self.ask("    Footprint numbered like the datasheet", ["y", "n"],
                        labels={"y": "yes, the same", "n": "no, different"})
        text = R.set_variant_key(text, "pad_layout",
                                 "same" if same == "y" else "differs")
        atomic.write_text(path, text)

    def connection_result(self, path, ln):
        """Compare what was just answered with the symbol's drawing."""
        var = _variant(path, ln.mpn)
        if not TD.asks_connections(var, ln.comp.pins):
            return
        symbol = getattr(ln, "symbol", None)
        found = P.connection_findings(var, symbol)
        if not found:
            if symbol is not None:
                self.tell("The symbol's drawing connects the same pins"
                          + (", with the dots the same way." if var.dots
                             else "."))
            return
        for f in found:
            if f["code"] == "connections_unread":
                self.do_connections_unread(TD.Item(
                    "connections_unread", ln.mpn, ln.refs, path, f["msg"], [f]), ln)
        bad = [f for f in found if f["code"] != "connections_unread"]
        if bad:
            self.do_connections(TD.Item("connections", ln.mpn, ln.refs, path,
                                        "; ".join(f["msg"] for f in bad), bad), ln)

    def show_symbol(self, ln):
        symbol = getattr(ln, "symbol", None)
        svg = symbol and SG.render_svg(symbol, os.path.join(
            os.path.dirname(self.cache), "symbols"))
        if not svg:
            return False
        if self.ui is not None:
            self.ui.show_image(svg, "symbol " + symbol.lib_id)
        else:
            self.say(f"    symbol: file://{urllib.parse.quote(svg)}")
            if self.auto_open:
                self.viewers.append(_open_file(svg))
        return True

    def do_connections_unread(self, it, ln):
        f = it.findings[0]
        shown = self.show_symbol(ln)
        self.tell(f"The datasheet connects {f['datasheet']} inside the part. "
                  f"bomcheck cannot follow the lines of this symbol, so compare "
                  f"by eye" + (": the picture is the symbol with its pin numbers."
                               if shown else f" ({ln.comp.libsource})."))
        if self.ask("    Does the symbol connect the same pins", ["y", "n"]) == "y":
            text = R.set_variant_key(open(it.path, encoding="utf-8").read(),
                                     "symbol_checked", ln.symbol.lib_id)
            atomic.write_text(it.path, text)
            R.sign(it.path, self.by, self.today)
            self.say(f"    recorded in {self.rel(it.path)}")
            return
        self.do_connections(TD.Item("connections", ln.mpn, ln.refs, it.path,
                                    "the symbol connects other pins than the "
                                    "datasheet"), ln)

    def do_connections(self, it, ln):
        self.tell(f"{it.detail}.")
        self.show_symbol(ln)
        self.decide(it)

    def do_polarity_switch(self, it, ln):
        var = _variant(it.path, ln.mpn)
        typed = ", ".join(f"pin {n} = {v!r}" for n, v in sorted(var.pins.items())
                          if v.strip())
        noun = {PO.POLAR_DIODE: "diode", PO.POLAR_CAP: "polarized capacitor",
                PO.BIDIRECTIONAL: "bidirectional diode"}[PO.classify(ln.comp)]
        self.tell(f"{' '.join(ln.refs)} is a {noun}. All that needs checking is "
                  f"what the marking on the part (band, dot) means. An earlier "
                  f"session asked for its pin names instead, and an unfinished "
                  f"answer was saved: {typed}.")
        if self.ask("    Discard that answer and go to the marking question",
                    ["y", "n"], "y") == "n":
            return self.do_stub_unfilled(TD.Item("stub_unfilled", ln.mpn,
                                                 ln.refs, it.path), ln)
        pdf = self.known_pdf(ln)
        path = R.write_polarity_stub(self.att, ln, _text_of(pdf) if pdf else "")
        if os.path.abspath(path) != os.path.abspath(it.path):
            os.remove(it.path)
        self.do_polarity_unfilled(TD.Item("polarity_unfilled", ln.mpn, ln.refs,
                                          path), ln)

    def do_draft_review(self, it, ln):
        var = _variant(it.path, ln.mpn)
        broken, why = P.structurally_disagrees(var, ln.comp.pins)
        self.show_table(var, ln.comp.pins)
        self.show_pdf(ln, var.pins.values())
        if broken:
            self.say(f"    The pins could not be read properly from the "
                     f"datasheet: {why}.")
            if self.ask("    Type them in by hand instead",
                        ["y", "n"], "y") == "y":
                self.redo_as_stub(it.path, ln)
            return
        self.legend("      y = yes, e = correct some pins")
        ok = self.ask("    Does the 'datasheet' column match the datasheet",
                      ["y", "e"], "y", labels={"e": "correct some pins"})
        if ok == "e":
            self.edit_table(it.path, ln)
        self.check_table(it.path, ln)
        self.do_table_partial(TD.Item("table_partial", ln.mpn, ln.refs, it.path), ln)
        self.sign_and_promote(it.path, ln)

    def do_pin_difference(self, it, ln):
        self.show_pdf(ln, _variant(it.path, ln.mpn).pins.values())
        if it.detail and not it.findings:
            self.say(f"    The pin list does not fit this package: {it.detail}")
            self.say(f"    Fix or delete {self.rel(it.path)} by hand.")
            return
        self.check_table(it.path, ln, it.findings)

    def do_alias_other_symbol(self, it, ln):
        """The table's aliases were accepted for symbol A; this board places
        B. Either B has A's pinout (add B to A's block), or B gets its own
        block, starting from A's names."""
        var = _variant(it.path, ln.mpn)
        sym = ln.comp.libsource
        names = {p.num: p.name for p in ln.comp.pins}
        pins = {f["pin"] for f in it.findings}
        block = max(var.other_blocks(sym),
                    key=lambda b: len(pins & set(b.pins)))
        other = ", ".join(block.symbols)
        nums = sorted((n for n in block.pins if n in names), key=P._pinsort)
        same = all(block.pins[n] == names[n] for n in nums)
        self.tell(f"Pin names in this table were accepted for {other}. This "
                  f"board places {sym}, so they do not apply to it.")
        self.say(f"    {'pin':>5}  {'datasheet':16} {other[:24]:24} {sym[:24]}")
        for n in nums:
            mark = "" if block.pins[n] == names[n] else "  <"
            self.say(f"    {n:>5}  {var.pins.get(n, '-') or '-':16} "
                     f"{block.pins[n]:24} {names[n]}{mark}")
        if same:
            self.tell("Both symbols name these pins the same.")
        ans = self.ask(f"    Is the pinout of {sym} the same as of {other}",
                       ["y", "n"], "y" if same else "n",
                       labels={"y": f"same: add {sym} to the block",
                               "n": "differs: go pin by pin"})
        if ans == "y":
            R.add_block_symbol(it.path, ln.mpn, block.symbols, sym)
            self.say(f"    {sym} added to the block for {other}")
            return
        self.tell(f"For each pin, the name {sym} may use for it. Enter takes "
                  f"the name accepted for {other}; - = no alias, the symbol "
                  f"must use the {_word(it)}'s name.")
        typed = {}
        for n in nums:
            ans = self.ask(f"    pin {n} ({_word(it)}: {var.pins.get(n) or '-'})",
                           default=block.pins[n],
                           extra=[("-", "no alias")])
            typed[n] = "" if ans == "-" else ans
        if typed == {n: block.pins[n] for n in nums}:
            R.add_block_symbol(it.path, ln.mpn, block.symbols, sym)
            self.say(f"    same names: {sym} added to the block for {other}")
            return
        # An empty block still says the other symbol's names were looked at,
        # so what differs now is a plain pin difference.
        R.add_alias_block(it.path, ln.mpn, sym)
        for n, name in typed.items():
            if name:
                DC.add_alias(it.path, ln.mpn, n, name, var.pins.get(n, ""), sym)
        self.say(f"    a new alias block for {sym} in {self.rel(it.path)}")

    def do_table_partial(self, it, ln, word="datasheet"):
        var = _variant(it.path, ln.mpn)
        gaps = sorted((p for p in ln.comp.pins
                       if not str(var.pins.get(p.num, "")).strip()),
                      key=lambda p: P._pinsort(p.num))
        if not gaps and len(var.pins) >= int(var.pin_count or 0):
            return
        if not gaps:
            self.say(f"    The pin list has more pins than the symbol "
                     f"({var.pin_count}). Check pin_count in {self.rel(it.path)}.")
            return
        self.say(f"    {len(gaps)} pin(s) of the symbol have no name from the "
                 f"{word} yet. Type them from the {word} (- for no "
                 f"connect, Enter = later).")
        if word == "datasheet":
            self.show_pdf(ln, var.pins.values())
        else:
            self.show_spec(it.path)
        for p in gaps:
            name = self.ask(f"    pin {p.num}")
            if name:
                put = R.set_pin_names if p.num in var.pins else R.add_pins
                put(it.path, {p.num: "NC" if name == "-" else name})
        if word == "datasheet":
            self.check_table(it.path, ln)
        else:
            self.check_table(it.path, ln, word=word, code="interface_difference")

    def do_unsigned(self, it, ln):
        doc = P.Document(it.path, tomlite.load(it.path))
        if doc.reviewed_by:
            return  # signed earlier in this session
        var = _variant(it.path, ln.mpn)
        self.say(f"    {len(var.pins)} pins from {doc.doc_id or 'the datasheet'}, "
                 f"read by {doc.extracted_by or '?'}. Nobody has checked them yet.")
        if self.ask("    Show it", ["y", "n"], "n") == "y":
            self.show_table(var, ln.comp.pins)
        self.show_pdf(ln, var.pins.values())
        if self.ask("    Have you checked these pins against the datasheet",
                    ["y", "n"]) == "y":
            R.sign(it.path, self.by, self.today)
            self.say(f"    marked as checked in {self.rel(it.path)}")

    # ---- interface tables -------------------------------------------------
    def interface_docs(self):
        return TD.interface_docs(self.att, self.project or None)

    def do_interface_unset(self, it, ln):
        c = ln.comp
        self.tell(it.detail[0].upper() + it.detail[1:] + ".")
        self.tell("bomcheck does not edit symbol libraries: add the field in "
                  "the symbol editor, then Update Symbols from Library in the "
                  "schematic. Until then the names stay open.")
        _f, other, _v = TD.find_interface(self.interface_docs(), "", c.libsource)
        if other is None:
            name = self.ask("    The interface's name, to type its table now "
                            "(e.g. SL23; Enter = later)")
            if name:
                self.fill_interface(ln, name)
        raise Skip()

    def do_interface_unfilled(self, it, ln):
        name = TD.interface_of(ln.comp)
        _f, _d, var = TD.find_interface(self.interface_docs(), name,
                                        ln.comp.libsource)
        if var is not None:
            return self.say("    signed earlier in this session")
        self.fill_interface(ln, name)

    def fill_interface(self, ln, name):
        """Type, or confirm, each pin's name from the interface spec, then
        compare with the symbol and sign. The symbol's names are not shown
        while typing: they are what the table checks."""
        c = ln.comp
        path = TD.interface_draft_path(self.att, name, c.libsource)
        if not os.path.isfile(path) and not self.start_interface(path, ln, name):
            return
        self.show_spec(path)
        hints = IF.hints_of(path)
        var = _variant(path, "")
        blank = [n for n in sorted(var.pins, key=P._pinsort)
                 if not var.pins[n].strip()]
        self.tell(f"Type each pin's signal name as the {name} spec gives it, "
                  f"never from the symbol (- = not connected, Enter = later).")
        if hints:
            self.tell("bomcheck read some names from a table in the spec. Each "
                      "one is offered as the answer: check it in the spec "
                      "before you press Enter, a misread table is still wrong.")
        pictured = self.show_pads(ln)
        if len([n for n in blank if n not in hints]) > 8:
            self.tell("Many pins with one name can go in one line first: "
                      "NAME = pins, e.g. GND = 2-50/2 (every second pin from 2 "
                      "to 50) or GND = 2 4 10-12. Enter = none or done.")
            while True:
                ans = self.ask("    shared name")
                if not ans:
                    break
                nm, nums, wrong = IF.parse_shared(ans, set(blank))
                if wrong or not nums:
                    self.tell("Write it as NAME = pins, e.g. GND = 2-50/2."
                              + (f" Not a blank pin of this part: "
                                 f"{', '.join(wrong)}." if wrong else ""))
                    continue
                R.set_pin_names(path, {n: "NC" if nm == "-" else nm for n in nums})
                blank = [n for n in blank if n not in nums]
                self.say(f"    {nm}: pin {', '.join(nums)}")
        left = []
        for n in blank:
            ans = self.ask(f"    pin {n}" + (f" (pad {n})" if pictured else ""),
                           default=hints.get(n, ""),
                           extra=[("-", "not connected")])
            if not ans:
                left.append(n)
                continue
            R.set_pin_names(path, {n: "NC" if ans == "-" else ans})
        if left:
            self.say(f"    Left empty: pin {', '.join(left)}. The table stays a "
                     f"draft in {self.rel(path)}; the next run asks for those "
                     f"pins again.")
            return
        self.check_table(path, ln, word="spec", code="interface_difference")
        self.sign_interface(path, ln)

    def start_interface(self, path, ln, name):
        """Ask for the spec and write a blank table, with names read from a
        markdown table in the spec as hints. -> False without a spec."""
        self.tell(f"No table for the {name} interface on {ln.comp.libsource} "
                  f"yet. Its reference is the {name} spec: a URL or a file "
                  f"(markdown is fine).")
        while True:
            spec = self.ask(f"    The {name} spec (URL or file; Enter = later)",
                            files=True)
            if not spec:
                self.say("    Without the spec there is nothing to type the "
                         "names from.")
                return False
            where = spec if IF.is_url(spec) else self.abspath(spec)
            if IF.is_url(where) or os.path.isfile(where):
                break
            self.say("    not a URL, and no such file")
        pins = {p.num for p in ln.comp.pins}
        hints, source = {}, ""
        if not IF.is_url(where) and not where.lower().endswith(".pdf"):
            with open(where, encoding="utf-8", errors="replace") as f:
                tables = [(h, {n: v for n, v in t.items() if n in pins})
                          for h, t in IF.spec_tables(f.read())]
            tables = [(h, t) for h, t in tables if t]
            pick = 0
            if len(tables) == 1:
                pick = 1
            elif tables:
                self.say("    The spec has several pin tables:")
                for k, (h, t) in enumerate(tables, 1):
                    self.say(f"      {k} = {h or 'a table'} ({len(t)} pins)")
                keys = [str(k) for k in range(1, len(tables) + 1)]
                ans = self.ask("    Which one is this symbol's pinout",
                               keys + ["n"], "n",
                               labels={"n": "none, type all"})
                pick = int(ans) if ans != "n" else 0
            if pick:
                heading, hints = tables[pick - 1]
                source = os.path.basename(where) + (
                    f", table under '{heading}'" if heading else "")
        IF.write_stub(path, name, ln.comp.libsource,
                      sorted(pins, key=P._pinsort), where, hints, source)
        return True

    def sign_interface(self, path, ln):
        R.sign(path, self.by, self.today)
        dst = R.promote(path, self.save_dir(ln, "interface"))
        doc = P.Document(dst, tomlite.load(dst))
        if doc.spec and not IF.is_url(doc.spec) and os.path.isabs(doc.spec):
            # Relative to the table, so the link survives another checkout.
            text = R.set_top_key(open(dst, encoding="utf-8").read(), "spec",
                                 os.path.relpath(doc.spec, os.path.dirname(dst)))
            atomic.write_text(dst, text)
            if _repo_root(os.path.dirname(doc.spec)) != \
                    _repo_root(os.path.dirname(dst)):
                self.tell("The spec file is in another repository than the "
                          "table, so other checkouts may not find it. Put its "
                          f"URL in spec = ... in {self.rel(dst)}.")
        self.say(f"    saved, checked by {self.by or '?'}: {self.rel(dst)}")
        if self.library and dst.startswith(self.library + os.sep):
            self.saved_in_library.append(dst)
        return dst

    def show_spec(self, path):
        """Point at the interface spec a table names: a URL or a file."""
        doc = P.Document(path, tomlite.load(path))
        where = IF.resolve_spec(doc.spec, os.path.dirname(path))
        if not where:
            self.tell(f"bomcheck cannot open the spec this table names "
                      f"(spec = {doc.spec!r} in {self.rel(path)}).")
            return
        if self.ui is not None:
            return self.ui.show_doc(where, f"{doc.interface} spec")
        self.say("    spec: " + (where if IF.is_url(where)
                                 else "file://" + urllib.parse.quote(where)))
        if self.auto_open and where not in self.opened:
            self.opened.add(where)
            self.viewers.append(_open(where) if where.lower().endswith(".pdf")
                                else _spawn(_any_cmd(where)))

    def do_interface_difference(self, it, ln):
        self.show_spec(it.path)
        if it.code == "interface_structure":
            self.say(f"    The interface table does not fit this symbol: "
                     f"{it.detail}")
            self.say(f"    Fix or delete {self.rel(it.path)} by hand.")
            raise Skip()
        self.show_symbol(ln)
        self.check_table(it.path, ln, it.findings, word="spec",
                         code="interface_difference")

    def do_interface_partial(self, it, ln):
        self.do_table_partial(it, ln, word="spec")

    def do_interface_unsigned(self, it, ln):
        doc = P.Document(it.path, tomlite.load(it.path))
        if doc.reviewed_by:
            return  # signed earlier in this session
        var = _variant(it.path, "")
        self.say(f"    {len(var.pins)} names from the {doc.interface} spec, typed "
                 f"by {doc.extracted_by or '?'}. Nobody has checked them yet.")
        if self.ask("    Show it", ["y", "n"], "n") == "y":
            self.show_table(var, ln.comp.pins, word="spec")
        self.show_spec(it.path)
        if self.ask("    Have you checked these names against the spec",
                    ["y", "n"]) == "y":
            R.sign(it.path, self.by, self.today)
            self.say(f"    marked as checked in {self.rel(it.path)}")
        else:
            raise Skip()

    # ---- polarity ---------------------------------------------------------
    def do_polarity_unfilled(self, it, ln):
        klass = PO.classify(ln.comp)
        pdf = self.known_pdf(ln)
        mark, evidence, page = PO.datasheet_hint(_text_of(pdf)) if pdf else ("", "", 0)
        self.tell({PO.POLAR_DIODE: "One-way diode: it has a cathode.",
                   PO.POLAR_CAP: "Polarized capacitor: it has a plus.",
                   PO.BIDIRECTIONAL: "Two-way diode: it has no cathode."}[klass])
        family = _FAMILY_EXCEPTION.search(evidence)
        if family:
            evidence = evidence[:family.end()]
        if evidence:
            self.tell(f'Datasheet, page {page}: "{evidence}"')
        if family:
            self.tell(_family_exception(klass))
        self.show_footprints(ln)
        self.show_pdf(ln, page=page)
        kind, marked = self.ask_marking(klass, mark, evidence)
        text = R.set_variant_key(open(it.path, encoding="utf-8").read(),
                                 "marked", marked)
        if kind:
            text = R.set_variant_key(text, "mark", kind)
        atomic.write_text(it.path, text)
        for bad in TD.datasheet_items(ln, _variant(it.path, ln.mpn)):
            self.tell(f"Note: {bad.detail} ({' '.join(bad.refs)}).")
        self.sign_and_promote(it.path, ln)

    def ask_marking(self, klass, hint, evidence):
        """Two questions: what mark the datasheet shows on the part, then
        which pin it is on. -> (kind of mark, marked pin: plus, minus,
        cathode, anode or none)."""
        self.tell("Reason: the assembler puts the mark on the part next to the "
                  "mark on the footprint.")
        kinds = {"n": "", "p": "plus sign", "b": "band", "d": "dot", "o": "other"}
        labels = {"n": "no polarity", "p": "a + sign", "b": "a band or bar",
                  "d": "a dot", "o": "other mark"}
        words = evidence.lower()
        default = ("n" if klass == PO.BIDIRECTIONAL else
                   "b" if re.search(r"\b(band|stripe|bar)\b", words) else
                   "d" if re.search(r"\bdot\b", words) else "")
        ans = self.ask("    What mark does the datasheet show on the part",
                       list(kinds), default, labels=labels)
        if ans == "n":
            return "", "none"
        kind = kinds[ans]
        if ans == "o":
            kind = "other: " + (self.ask("    describe the mark") or "?")
        if klass == PO.POLAR_CAP and ans == "p":
            return kind, "plus"
        pins = ({"p": "plus", "m": "minus"} if klass == PO.POLAR_CAP
                else {"c": "cathode", "a": "anode"})
        pins["o"] = "other"
        shown = {"p": "the + pin", "m": "the − pin", "c": "the cathode",
                 "a": "the anode", "o": "other"}
        default = next((k for k, v in pins.items() if v == hint), "")
        what = {"plus sign": "+ sign", "band": "band or bar"}.get(kind, kind)
        pin = self.ask(f"    Which pin is the {what} on", list(pins), default,
                       labels={k: shown[k] for k in pins})
        if pin == "o":
            note = self.ask("    describe it (for example: the datasheet does "
                            "not say)") or "?"
            return kind, "other: " + note
        return kind, pins[pin]

    def do_polarity(self, it, ln):
        self.tell(f"{' '.join(it.refs)}: {it.detail}.")
        self.show_footprints(ln, it.refs)
        self.decide(it)

    def do_mixed_symbols(self, it, ln):
        self.tell(f"One part number, several symbols: {it.detail}. Use one "
                  f"symbol for all of them, or the checks differ per placement.")
        self.decide(it)

    def decide(self, it, pins=()):
        """A design finding: fix it, or waive it with a reason."""
        rev = f"rev {self.rev}" if self.rev else "this revision"
        self.legend("      f = fix it in the design now (stays open until fixed)")
        self.legend(f"      k = known issue of {rev}, fix in the next revision")
        if not pins:
            self.legend("      a = accept it for good")
        ans = self.ask("    This finding", ["f", "k"] + ([] if pins else ["a"]),
                       labels={"f": "fix now", "k": "known issue",
                               "a": "accept for good"})
        if ans == "f":
            return
        note = ""
        while not note:
            note = self.ask("    why (a note is required)")
        WV.add(self.att, it.code, it.refs, note, self.by, self.today,
               pins=pins, until=self.rev if ans == "k" else "",
               about=_about(self.line, it.detail))
        self.say(f"    recorded in {self.rel(WV.path_of(self.att))}")

    def do_waiver_expired(self, it, ln):
        w = it.findings[0]
        self.tell(f"{it.detail}.")
        self.tell(f"Its note: {w.note}")
        self.say("      f = fix it now (it stays on the list until fixed)")
        self.say(f"      k = still a known issue in rev {self.rev}, carry it over")
        if self.ask("    This finding", ["f", "k"],
                    labels={"f": "fix now", "k": "carry over"}) == "k":
            WV.renew(self.att, w, self.rev, self.by, self.today)
            self.say(f"    updated in {self.rel(WV.path_of(self.att))}")

    def do_waiver_stale(self, it, ln):
        w = it.findings[0]
        self.tell(f"{it.detail}. Its note: {w.note}")
        if self.ask("    r = remove it", ["r"], "r", labels={"r": "remove"}) == "r":
            WV.remove(self.att, w)
            self.say(f"    removed from {self.rel(WV.path_of(self.att))}")

    def do_waiver_invalid(self, it, ln):
        self.tell(f"{it.detail}. Edit {self.rel(WV.path_of(self.att))} by hand.")

    def do_footprint_mark(self, it, ln):
        name = it.detail
        self.tell(f"bomcheck cannot tell which pad {name} marks. Look at the "
                  f"picture: which pad is the band, bar or + next to?")
        self.show_footprints(ln, it.refs)
        ans = self.ask("    marked pad (1, 2, or n = no marking)", ["1", "2", "n"],
                       labels={"1": "pad 1", "2": "pad 2", "n": "no marking"})
        FP.add_signed(self.att, name, "" if ans == "n" else ans, self.by, self.today)
        self.say(f"    recorded in {self.rel(FP.signed_path(self.att))}")

    def show_pads(self, ln):
        """Open a picture of the board footprint with its pad numbers, for
        typing names from a package drawing. Not the symbol: its names are
        what the answers check. -> True if there is a picture."""
        import glob
        pcbs = glob.glob(os.path.join(self.project, "*.kicad_pcb"))
        fp = FP.from_pcb(pcbs[0], ln.refs[0]) if len(pcbs) == 1 else None
        svg = fp and FP.render_svg(fp, os.path.join(os.path.dirname(self.cache),
                                                    "footprints"))
        if not svg:
            return False
        if self.ui is not None:
            self.ui.show_image(svg, fp.name)
        else:
            self.say(f"    footprint: file://{urllib.parse.quote(svg)}")
            if self.auto_open:
                self.viewers.append(_open_file(svg))
        return True

    def show_footprints(self, ln, refs=None):
        """Name each footprint's marked pad, and open a picture of it."""
        marks = FP.board_marks(self.project, self.att)
        seen = set()
        for ref in refs or ln.refs:
            name, pad, why, fp = marks.get(ref, ("", "", "", None))
            if not name or name in seen:
                continue
            seen.add(name)
            self.tell(f"Footprint {name}: " + (f"marks pad {pad} ({why})." if pad
                                                else f"{why}."))
            svg = FP.render_svg(fp, os.path.join(os.path.dirname(self.cache),
                                                 "footprints"))
            if svg and self.ui is not None:
                self.ui.show_image(svg, name)
            elif svg:
                self.say(f"    picture: file://{urllib.parse.quote(svg)}")
                if self.auto_open:
                    self.viewers.append(_open_file(svg))

    # ---- shared steps -----------------------------------------------------
    def check_table(self, path, ln, findings=None, word="datasheet",
                    code="pin_difference"):
        """Settle every pin where the table and the symbol disagree. `word`
        names the reference (datasheet or spec), `code` the waiver."""
        var = _variant(path, ln.mpn)
        if findings is None:
            findings = [f for f in P.compare(var, ln.comp.pins,
                                             ln.comp.libsource)
                        if f["code"] in TD.DISAGREE]
        mixed = [f for f in findings if f["code"] == "pin_unit_mismatch"]
        findings = [f for f in findings if f["code"] != "pin_unit_mismatch"]
        if mixed:
            self.settle_units(path, ln, mixed)
        if not findings:
            return
        self.say(f"    {len(findings)} pin(s) differ between the {word} table "
                 f"and the symbol:")
        self.say(f"    {'pin':>5}  {word:24} symbol")
        for f in findings:
            self.say(f"    {f['pin']:>5}  {f['datasheet'] or '-':24} {f['symbol']}")
        if len(findings) > 1:
            self.say("      a = all the same pins, the symbol just names them "
                     "differently")
            self.say("      1 = go through them one by one")
            if self.ask("    These pins", ["a", "1"],
                        labels={"a": "all the same", "1": "one by one"}) == "a":
                for f in findings:
                    DC.add_alias(path, ln.mpn, f["pin"], f["symbol"],
                                 f["datasheet"], ln.comp.libsource)
                return
        for f in findings:
            self.say(f"\n    pin {f['pin']}: {word} {f['datasheet']!r}, "
                     f"symbol {f['symbol']!r}")
            self.say("      a = same pin, the symbol just names it differently")
            self.say(f"      t = the {word} column was read wrong, type the right name")
            self.say("      y = the symbol is wrong, fix it now (stays open until fixed)")
            rev = f"rev {self.rev}" if self.rev else "this revision"
            self.say(f"      k = the symbol is wrong, known issue of {rev}")
            ans = self.ask(f"    pin {f['pin']}", ["a", "t", "y", "k"],
                           labels={"a": "same pin", "t": "retype name",
                                   "y": "fix symbol now", "k": "known issue"})
            if ans == "a":
                DC.add_alias(path, ln.mpn, f["pin"], f["symbol"],
                             f["datasheet"], ln.comp.libsource)
            elif ans == "t":
                R.set_pin_names(path, {f["pin"]: self.ask("      name")})
            elif ans == "k":
                note = ""
                while not note:
                    note = self.ask("      why (a note is required)")
                WV.add(self.att, code, ln.refs, note, self.by,
                       self.today, pins=[f["pin"]], until=self.rev,
                       about=_about(ln, f["msg"]))

    def settle_units(self, path, ln, mixed):
        """A dual part where one symbol unit holds pins of both devices."""
        for msg in dict.fromkeys(f["msg"] for f in mixed):
            self.tell(msg[0].upper() + msg[1:] + ".")
        self.tell("So either a name was typed against the wrong pin, or the "
                  "symbol mixes the two devices.")
        rev = f"rev {self.rev}" if self.rev else "this revision"
        ans = self.ask("    These pins", ["t", "y", "k"],
                       labels={"t": "retype the names", "y": "fix symbol now",
                               "k": f"known issue of {rev}"})
        pins = [f["pin"] for f in mixed]
        if ans == "t":
            for p in pins:
                R.set_pin_names(path, {p: self.ask(f"      pin {p} name")})
        elif ans == "k":
            note = ""
            while not note:
                note = self.ask("      why (a note is required)")
            WV.add(self.att, "pin_difference", ln.refs, note, self.by,
                   self.today, pins=pins, until=self.rev,
                   about=_about(ln, mixed[0]["msg"]))

    def sign_and_promote(self, path, ln):
        """The answers just given are the check, so no second confirmation.
        The document is recorded as the datasheet that was shown."""
        doc = P.Document(path, tomlite.load(path))
        field = ln.comp.field("Datasheet").strip()
        kind = D.field_kind(field)
        # A project path means nothing to another board reading this table.
        source = (field if kind == "url" else os.path.basename(field) if kind == "file"
                  else "") or os.path.basename(self.known_pdf(ln) or "")
        if not doc.doc_id and source:
            text = R.set_top_key(open(path, encoding="utf-8").read(),
                                 "doc_id", source)
            atomic.write_text(path, text)
        R.sign(path, self.by, self.today)
        dst = R.promote(path, self.save_dir(ln))
        self.say(f"    saved, checked by {self.by or '?'}: {self.rel(dst)}")
        if self.library and dst.startswith(self.library + os.sep):
            self.saved_in_library.append(dst)
        return dst

    def save_dir(self, ln, kind="datasheet"):
        """The library for a part from a component library or KiCad's own,
        the project for a project-local symbol. Asked, with that as the
        default, unless --save-to says. `kind`: datasheet or interface."""
        own = os.path.join(self.att, "reference", kind)
        if not self.library:
            return own
        default = LIB.scope_of(ln.comp.libsource, self.project)
        where = self.save_to if self.save_to in ("library", "project") else ""
        if not where:
            name = os.path.basename(self.library)
            ans = self.ask("    Save the signed table in", ["l", "p"],
                           "l" if default == "library" else "p",
                           labels={"l": f"library ({name}), for every board",
                                   "p": "this project only"})
            where = "library" if ans == "l" else "project"
        if where != "library":
            return own
        return (LIB.interface_dir(self.library) if kind == "interface"
                else LIB.signed_dir(self.library))

    def edit_table(self, path, ln):
        self.say("    Pin number and the datasheet's name, empty line to finish.")
        while True:
            num = self.ask("    pin")
            if not num:
                return
            R.set_pin_names(path, {num: self.ask(f"    pin {num} name")})

    def redo_as_stub(self, path, ln):
        os.remove(path)
        stub = os.path.join(self.att, "reference", "draft", R.stub_name(ln.mpn))
        lines = [f'doc_id       = ""', 'manufacturer = ' +
                 tomlite.quote(ln.comp.field("Manufacturer")),
                 'origin       = "manual"', 'extracted_by = ""', "",
                 "  [[variant]]", f"  part         = {tomlite.quote(ln.mpn)}",
                 f"  package      = {tomlite.quote(ln.comp.footprint.partition(':')[2])}",
                 f"  mpns         = [{tomlite.quote(ln.mpn)}]",
                 '  confidence   = "manual"', f"  pin_count    = {len(ln.comp.pins)}",
                 "", "    [variant.pins]"]
        lines += [f'    {p.num} = ""' for p in
                  sorted(ln.comp.pins, key=lambda p: P._pinsort(p.num))]
        atomic.write_text(stub, "\n".join(lines) + "\n")
        it = TD.Item("stub_unfilled", ln.mpn, ln.refs, stub)
        self.do_stub_unfilled(it, ln)

    # ---- showing things ---------------------------------------------------
    def show_table(self, var, pins, word="datasheet"):
        sym = {p.num: p.name for p in pins}
        unit = {p.num: f" (unit {p.unit})" for p in pins if getattr(p, "unit", "")}
        nums = sorted(set(var.pins) | set(sym), key=P._pinsort)
        self.say(f"    {'pin':>5}  {word:24} symbol   (< = named differently)")
        for n in nums:
            ds, sy = var.pins.get(n, "-"), sym.get(n, "-")
            mark = "" if P._norm_pin_name(ds) == P._norm_pin_name(sy) else "  <"
            self.say(f"    {n:>5}  {ds:24} {sy}{unit.get(n, '')}{mark}")

    def show_hints(self, path):
        """Print the drawing labels a stub quotes. -> the label words."""
        hints = [ln for ln in open(path, encoding="utf-8").read().split("\n")
                 if ln.startswith("#     ")]
        if hints:
            self.say("    Labels found in the package drawing (names only, NOT "
                     "the numbering):")
            for h in hints:
                self.say("      " + h[1:].strip())
        return [w for h in hints for w in h[1:].split()]

    def known_pdf(self, ln):
        """The datasheet for this part: one given this session, else one an
        answer put in decisions-fields.csv (not yet in the schematic), else
        the schematic's own. -> path, or ""."""
        if self.session_pdf.get(ln.mpn):
            return self.session_pdf[ln.mpn]
        pending = [e.value for e in DC.parse_fields(self.fields_csv)
                   if e.field == "Datasheet" and (e.part == ln.mpn
                                                  or set(e.refs) & set(ln.refs))]
        if pending:
            src = D.field_source(pending[-1], self.project)
            f = D.fetch(ln.mpn, "pending", src, self.cache) if src else None
            if f and f.ok:
                return f.path
        return _find_pdf(ln.mpn, ln.comp, self.project, self.cache)

    def show_pdf(self, ln, names=(), force=False, page=0):
        """Point at the datasheet: a clickable link, and open it once per line."""
        pdf = self.known_pdf(ln)
        if not pdf:
            if self.ui is not None:
                return self.ui.show_missing(self.datasheet_link(ln),
                                            ln.comp.field("Manufacturer").strip())
            if force:
                self.tell("No datasheet found in the cache or the project.")
            return
        page = page or _best_page(_text_of(pdf), names,
                                  _package_word(ln.comp.footprint))
        if self.ui is not None:
            return self.ui.show_pdf(pdf, page)
        self.say(f"    datasheet: file://{urllib.parse.quote(pdf)}#page={page}"
                 + ("" if self.tui else "   (o opens it)"))
        if force or (self.auto_open and ln.mpn not in self.opened):
            self.opened.add(ln.mpn)
            self.viewers.append(_open(pdf, page))

def _variant(path, mpn):
    """The part's variant; for an interface table (keyed to symbols, one
    variant per file) that one variant."""
    doc = P.Document(path, tomlite.load(path))
    return next((v for v in doc.variants if mpn in v.mpns), None) or next(
        v for v in doc.variants if not v.mpns)


def _word(it):
    """What the table's names come from."""
    return ("spec" if os.path.basename(os.path.dirname(it.path)) == "interface"
            else "datasheet")


def _connector_like(ln):
    """Worth asking whether the datasheet only numbers the pins: a symbol
    with an interface, a J/P/X/CN designator, or a connector footprint."""
    c = ln.comp
    return bool(TD.interface_of(c)) or any(
        re.match(r"(J|P|X|CN|CON)\d", r) for r in ln.refs) or \
        c.footprint.lower().startswith("connector")


def _about(ln, detail):
    """What a waiver was about, in words, for whoever reads it next revision."""
    if ln is None:
        return detail
    syms = sorted({c.libsource for c in ln.comps})
    fps = sorted({c.footprint for c in ln.comps})
    return f"{ln.mpn}; {', '.join(syms)}; {', '.join(fps)}; {detail}"


def _csv(x):
    x = str(x)
    return '"' + x.replace('"', '""') + '"' if any(ch in x for ch in ',"\n') else x


def _name_file():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "bomcheck", "name")


def _remembered_name():
    try:
        return open(_name_file(), encoding="utf-8").read().strip()
    except OSError:
        return ""


def _remember_name(name):
    if not name or name == _remembered_name():
        return
    try:
        os.makedirs(os.path.dirname(_name_file()), exist_ok=True)
        atomic.write_text(_name_file(), name + "\n")
    except OSError:
        pass   # asked again next time, nothing worse


def _git_user(project):
    try:
        r = subprocess.run(["git", "-C", project, "config", "user.name"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except Exception:                                            # noqa: BLE001
        return ""


def _find_pdf(mpn, comp, project, cache):
    local = os.path.join(project, "datasheets")
    for origin, src in D.resolve(mpn, symbol_url=D.field_source(
                                     comp.field("Datasheet"), project),
                                 local_dir=local):
        f = D.fetch(mpn, origin, src, cache)
        if f.ok and D.verify(mpn, f.text)["verdict"] in ("confirmed", "family-only"):
            return f.path
    return ""


def _same_file(a, b):
    with open(a, "rb") as fa, open(b, "rb") as fb:
        return fa.read() == fb.read()


def _repo_root(path):
    try:
        r = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=5)
        return os.path.abspath(r.stdout.strip()) if r.returncode == 0 else ""
    except Exception:                                            # noqa: BLE001
        return ""


def _guess_pdf(mpn, project, cache=None):
    """A sensible default: a project datasheet about this part, else the
    newest download. Project files are tried most name-similar first, and
    read through the cache."""
    import glob
    local = os.path.join(project, "datasheets")
    cache = cache or D.default_cache()
    for origin, src in D.resolve(mpn, local_dir=local):
        if origin != "local-archive":
            continue
        f = D.fetch(mpn, origin, src, cache)
        if f.ok and D.verify(mpn, f.text)["verdict"] in ("confirmed", "family-only"):
            return src
    downloads = glob.glob(os.path.expanduser("~/Downloads/*.pdf"))
    return max(downloads, key=os.path.getmtime) if downloads else ""


_PIN_HEADING = re.compile(r"pin (configuration|functions?|descriptions?|assignments?|"
                          r"definitions?)|pin-?out|connection diagram|"
                          r"signal descriptions?", re.I)
_DRAWING = re.compile(r"package (drawing|outline|dimensions)", re.I)
_TOC_LEADER = re.compile(r"(?:\.\s?){6,}\s*\d+\s*$", re.M)


_FAMILY_EXCEPTION = re.compile(r"\bexcept\b.*?\bbi(polar|-?directional)\w*", re.I)


def _family_exception(klass):
    """A family datasheet: "band denotes cathode except bipolar". Which half
    of that sentence applies follows from the symbol."""
    if klass == PO.BIDIRECTIONAL:
        return "The exception applies to this part. Expected answer: nothing."
    return ("The exception is for two-way parts (...CA). Expected answer: "
            "the cathode.")


def _text_of(pdf):
    txt = os.path.splitext(pdf)[0] + ".txt"
    if os.path.isfile(txt):
        return open(txt, encoding="utf-8").read()
    return D.pdf_to_text(pdf)[0]


def _best_page(text, names=(), package=""):
    """The page most likely to hold the pinout: most of the given pin names,
    then pin headings, a mention of the board's package, and drawings last.
    Contents pages are skipped. 1-based; 1 when nothing stands out."""
    names = {n.upper() for n in names if n and len(n) > 1}
    pkg = package.upper()
    best, best_score = 1, 0
    for i, page in enumerate(text.split("\f"), 1):
        if len(_TOC_LEADER.findall(page)) > 5:
            continue
        words = set(re.findall(r"[A-Z0-9_+#/-]+", page.upper()))
        score = (2 * len(names & words) + 3 * len(_PIN_HEADING.findall(page))
                 + len(_DRAWING.findall(page)) + (2 if pkg and pkg in words else 0))
        if score > best_score:
            best, best_score = i, score
    return best


def _package_word(footprint):
    """SOIC from Package_SO:SOIC-8_5.23x5.23mm, SOT from SOT-363_SC-70-6."""
    m = re.match(r"[A-Za-z]+", footprint.partition(":")[2])
    return m.group(0) if m else ""


def _viewer_cmd(pdf, page):
    """Open at a page where the default viewer allows it."""
    try:
        app = subprocess.run(["xdg-mime", "query", "default", "application/pdf"],
                             capture_output=True, text=True, timeout=5).stdout.lower()
    except Exception:                                            # noqa: BLE001
        app = ""
    for name, flag in (("evince", "-i"), ("atril", "-i"), ("xreader", "-i"),
                       ("okular", "-p"), ("zathura", "-P")):
        if name in app and shutil.which(name):
            return [name, flag, str(page), pdf]
    for cmd in ("xdg-open", "open"):
        if shutil.which(cmd):
            return [cmd, pdf]
    return None


def _image_cmd(path):
    """The default image viewer started directly, in a window of its own, so
    it can be closed again. xdg-open would hand the file over and exit."""
    try:
        app = subprocess.run(["xdg-mime", "query", "default", "image/svg+xml"],
                             capture_output=True, text=True, timeout=5).stdout.lower()
    except Exception:                                            # noqa: BLE001
        app = ""
    for name, flags in (("eog", ["--new-instance"]), ("ristretto", []),
                        ("gthumb", []), ("gwenview", []), ("loupe", [])):
        if name in app and shutil.which(name):
            return [name, *flags, path]
    for cmd in ("xdg-open", "open"):
        if shutil.which(cmd):
            return [cmd, path]
    return None


def _spawn(cmd):
    """-> Popen, or None. Own session, so closing it takes its children."""
    if not cmd:
        return None
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)


def _open_file(path):
    return _spawn(_image_cmd(path))


def _open(pdf, page=1):
    return _spawn(_viewer_cmd(pdf, page))


def _any_cmd(target):
    """Hand a URL or a non-PDF file (a markdown spec) to the desktop."""
    for cmd in ("xdg-open", "open"):
        if shutil.which(cmd):
            return [cmd, target]
    return None
