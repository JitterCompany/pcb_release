#!/usr/bin/env python3
"""The wizard in a browser tab: the question, the datasheet page and the
footprint picture side by side. Stdlib only.

The wizard runs in a thread of its own and asks exactly as it does on a
terminal; `WebUI.ask` waits until the page answers. A local http.server
serves one page, a long-polled /state, /answer, and only the files the
wizard asked to show. The browser's own PDF viewer shows the datasheet
(#page=N); pdftoppm, which comes with the pdftotext bomcheck needs anyway,
gives a page image for viewers that ignore #page.

Bound to 127.0.0.1 with a random token in every path, so another user on the
same machine cannot answer for you.
"""
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
POLL_S = 25
MAX_DROP = 200 * 1024 * 1024
PORT = 8790        # fixed, so the address can be bookmarked; any free one if taken


def stable_token():
    """The secret in the address, made once per user and kept where only that
    user can read it. 127.0.0.1 is reachable by other users on the machine and
    by any web page open in the browser, so the address cannot be guessable;
    it can stay the same between runs."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    path = os.path.join(base, "bomcheck", "web-token")
    try:
        return open(path, encoding="utf-8").read().strip()
    except OSError:
        pass
    token = secrets.token_urlsafe(16)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
    except OSError:
        pass     # a new address next run, nothing worse
    return token


class WebUI:
    """The front end the wizard talks to (see Wizard.ui): ask, say, part,
    show_pdf, show_image, end_part. Every call changes `state()` and wakes
    the pages waiting for it."""

    def __init__(self, echo=None, project=""):
        # The terminal keeps a plain record of the session, as without --gui.
        self.echo = echo or (lambda text: print(text, flush=True))
        self.cv = threading.Condition()
        self.project = project   # shown in the title: several may run at once
        self.version = 0
        self.log = []          # [{"part": n, "kind": say|answer, "text": ...}]
        self.parts = []        # [{"n", "total", "mpn", "refs", "about"}]
        self.question = None
        self.answer = None
        self.files = {}        # id -> path; nothing else is ever served
        self.pdf = None
        self.images = []
        self.done = False
        self.summary = None    # what is left, shown on the finish page
        self.closed = False
        self.pages_dir = tempfile.mkdtemp(prefix="bomcheck-pages-")
        self.drops = 0
        self.page_images = bool(shutil.which("pdftoppm"))

    def _changed(self):
        self.version += 1
        self.cv.notify_all()

    def _part_no(self):
        return self.parts[-1]["n"] if self.parts else 0

    # ---- what the wizard calls --------------------------------------------
    def say(self, text):
        with self.cv:
            for line in str(text).split("\n"):
                self.log.append({"part": self._part_no(), "kind": "say",
                                 "text": line})
            self._changed()
        self.echo(text)

    def part(self, n, total, mpn, refs, about, maker=""):
        with self.cv:
            self.parts.append({"n": n, "total": total, "mpn": mpn,
                               "refs": refs, "about": about, "maker": maker})
            self._changed()
        self.echo(f"\n=== {n}/{total}  {mpn}  {refs}\n    {about}")

    def ask(self, prompt, buttons, default, text, actions):
        """-> the value of the button pressed, or the typed text. ":q" once
        the server stops, so the wizard ends the way Esc ends it."""
        with self.cv:
            qid = self.version + 1
            self.question = {"id": qid, "prompt": prompt, "default": default,
                             "text": text,
                             "buttons": [list(b) for b in buttons],
                             "actions": [list(a) for a in actions]}
            self.answer = None
            self._changed()
            while self.answer is None and not self.closed:
                self.cv.wait()
            ans = ":q" if self.answer is None else self.answer
            shown = next((lbl for v, lbl in list(buttons) + list(actions)
                          if v == ans), ans or default or "-")
            self.question = None
            if ans.startswith(":drop "):
                shown = "dropped " + (ans[6:] if "://" in ans
                                      else os.path.basename(ans[6:]))
            if ans != ":o":
                self.log.append({"part": self._part_no(), "kind": "answer",
                                 "text": f"{prompt}: {shown}"})
            self._changed()
        self.echo(f"    {prompt}: {shown}")
        return ans

    def show_pdf(self, path, page):
        with self.cv:
            fid = self._register(path)
            # Named after the part: the viewer shows the last path segment.
            mpn = self.parts[-1]["mpn"] if self.parts else ""
            name = re.sub(r"[^A-Za-z0-9._-]", "_", mpn) + ".pdf" if mpn \
                else os.path.basename(path)
            self.pdf = {"id": fid, "url": f"file/{fid}/{name}", "page": page,
                        "name": f"{name} ({os.path.basename(path)})"}
            self._changed()

    def show_doc(self, where, name=""):
        """A document that is not a part's datasheet: an interface spec, as
        a URL or a file (markdown is shown as plain text)."""
        with self.cv:
            if where.lower().startswith(("http://", "https://")):
                self.pdf = {"remote": where, "page": 0, "maker": "",
                            "name": name or where, "doc": True}
            else:
                fid = self._register(where)
                base = os.path.basename(where)
                self.pdf = {"id": fid, "url": f"file/{fid}/{base}", "page": 1,
                            "name": f"{name} ({base})" if name else base,
                            "doc": not where.lower().endswith(".pdf")}
            self._changed()

    def show_image(self, path, name=""):
        with self.cv:
            fid = self._register(path)
            if all(i["id"] != fid for i in self.images):
                self.images.append({"id": fid, "url": f"file/{fid}",
                                    "name": name or os.path.basename(path)})
            self._changed()

    def show_missing(self, url="", maker=""):
        """No downloaded PDF: show the URL itself (if the site allows it in
        a frame), and ask for the file to be dropped."""
        with self.cv:
            self.pdf = {"remote": url, "page": 0, "maker": maker,
                        "name": url or "No datasheet found"}
            self._changed()

    def end_part(self):
        with self.cv:
            self.pdf, self.images = None, []
            self._changed()

    # ---- what the server calls --------------------------------------------
    def _register(self, path):
        path = os.path.abspath(path)
        for fid, p in self.files.items():
            if p == path:
                return fid
        fid = f"{len(self.files) + 1}{os.path.splitext(path)[1].lower()}"
        self.files[fid] = path
        return fid

    def submit(self, qid, value):
        with self.cv:
            if not self.question or self.question["id"] != qid:
                return False          # answered already, in another tab
            self.answer = str(value)
            self._changed()
            return True

    def drop(self, value):
        """A PDF path or URL dropped on the page answers the open question
        with ":drop <value>". -> False when nothing is being asked."""
        with self.cv:
            if not self.question:
                return False
            self.answer = ":drop " + value
            self._changed()
            return True

    def finish(self, summary=None):
        with self.cv:
            self.done, self.question = True, None
            self.summary = summary
            self._changed()

    def close(self):
        """Stop waiting: a pending question answers ":q"."""
        with self.cv:
            self.closed = True
            self._changed()

    def state(self, seen=-1, timeout=POLL_S):
        with self.cv:
            if seen == self.version and not self.done:
                self.cv.wait(timeout)
            return {"version": self.version, "log": self.log,
                    "project": self.project,
                    "parts": self.parts, "question": self.question,
                    "pdf": self.pdf, "images": self.images, "done": self.done,
                    "page_images": self.page_images, "summary": self.summary}

    def page_png(self, fid, page):
        """-> path of a PNG of one PDF page, made once, or ""."""
        pdf = self.files.get(fid)
        if not pdf or not self.page_images or not pdf.lower().endswith(".pdf"):
            return ""
        out = os.path.join(self.pages_dir, f"{fid}-{page}")
        if not os.path.isfile(out + ".png"):
            subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-r",
                            "130", "-png", "-singlefile", pdf, out],
                           capture_output=True, timeout=60)
        return out + ".png" if os.path.isfile(out + ".png") else ""


class _Handler(BaseHTTPRequestHandler):
    server_version = "bomcheck"

    def log_message(self, *args):
        pass

    def _route(self):
        """-> the path after /<token>/, or None for a wrong token."""
        path, _, query = self.path.partition("?")
        parts = path.split("/", 2)
        if len(parts) < 2 or not secrets.compare_digest(parts[1],
                                                        self.server.token):
            return None, ""
        return (parts[2] if len(parts) > 2 else ""), query

    def _send(self, code, body, ctype, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass      # the tab was closed or reloaded while waiting

    def _json(self, obj):
        self._send(200, json.dumps(obj).encode(), "application/json")

    def _file(self, path):
        if not path or not os.path.isfile(path):
            return self._send(404, b"not found", "text/plain")
        with open(path, "rb") as f:
            body = f.read()
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") or path.lower().endswith((".md", ".rst")):
            # A spec in markdown: shown as text. Never as HTML, which could
            # run script next to the page that holds the token.
            ctype = "text/plain; charset=utf-8"
        self._send(200, body, ctype, [("Content-Disposition", "inline")])

    def do_GET(self):
        route, query = self._route()
        ui = self.server.ui
        if route is None:
            return self._send(404, b"not found", "text/plain")
        if route == "":
            with open(os.path.join(HERE, "webui.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if route == "state":
            q = dict(kv.partition("=")[::2] for kv in query.split("&") if kv)
            try:
                seen = int(q.get("v", "-1"))
            except ValueError:
                seen = -1
            return self._json(ui.state(seen))
        if route.startswith("file/"):
            return self._file(ui.files.get(route[5:].split("/")[0]))
        if route.startswith("page/"):
            fid, _, page = route[5:].partition("/")
            page = page.removesuffix(".png")
            return self._file(ui.page_png(fid, int(page)) if page.isdigit()
                              else "")
        self._send(404, b"not found", "text/plain")

    do_HEAD = do_GET

    def do_POST(self):
        route, _ = self._route()
        if route == "drop":
            return self._drop()
        if route != "answer":
            return self._send(404, b"not found", "text/plain")
        try:
            n = int(self.headers.get("Content-Length", "0"))
            msg = json.loads(self.rfile.read(n) or b"{}")
            ok = self.server.ui.submit(int(msg["id"]), msg["value"])
        except (ValueError, KeyError, TypeError):
            return self._send(400, b"bad request", "text/plain")
        self._json({"ok": ok})


    def _drop(self):
        """Body: a PDF (Content-Type application/pdf), or a URL as text."""
        ui = self.server.ui
        n = int(self.headers.get("Content-Length", "0") or 0)
        if not 0 < n <= MAX_DROP:
            return self._json({"ok": False, "why": "empty or too large"})
        body = self.rfile.read(n)
        if self.headers.get("Content-Type", "").startswith("application/pdf"):
            if not body.startswith(b"%PDF-"):
                return self._json({"ok": False, "why": "that is not a PDF"})
            name = urllib.parse.unquote(self.headers.get("X-Name", "dropped.pdf"))
            name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(name)) or "x"
            with ui.cv:
                ui.drops += 1
                d = os.path.join(ui.pages_dir, f"drop{ui.drops}")
            os.makedirs(d)
            value = os.path.join(d, name if name.lower().endswith(".pdf")
                                 else name + ".pdf")
            with open(value, "wb") as f:
                f.write(body)
        else:
            value = body.decode("utf-8", "replace").strip().split()[0] if \
                body.strip() else ""
            if value.startswith("file://"):
                # A downloaded PDF, as the browser's address bar shows it.
                value = urllib.parse.unquote(
                    urllib.parse.urlsplit(value).path)
                try:
                    with open(value, "rb") as f:
                        if f.read(5) != b"%PDF-":
                            return self._json({"ok": False,
                                               "why": "that file is not a PDF"})
                except OSError as e:
                    return self._json({"ok": False,
                                       "why": f"cannot read it ({e.strerror})"})
            elif not re.match(r"https?://\S+$", value):
                return self._json({"ok": False, "why": "not a PDF or a URL"})
        if not ui.drop(value):
            return self._json({"ok": False, "why": "wait for the next question"})
        self._json({"ok": True})


def serve(wizard, ui, port=None, open_browser=True, out=sys.stdout, token=None):
    """Run `wizard` with `ui` as its front end until it ends or Ctrl-C.
    -> the wizard's exit code."""
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", PORT if port is None else port),
                                    _Handler)
    except OSError:
        if port is not None:
            raise
        print(f"[bomcheck] port {PORT} is taken (another wizard?); using a free "
              f"one, so the address differs this time.", file=out)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.daemon_threads = True
    httpd.ui, httpd.token = ui, token or stable_token()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/{httpd.token}/"
    result = {"rc": 0}

    def run():
        try:
            result["rc"] = wizard.run()
        finally:
            try:
                summary = wizard.summary()
            except Exception:                                    # noqa: BLE001
                summary = None
            ui.finish(summary)
            # Let the page fetch the final state before the server goes.
            threading.Timer(1.0, httpd.shutdown).start()

    print(f"[bomcheck] wizard in the browser: {url}\n"
          f"[bomcheck] Ctrl-C here stops it; answers are saved as you go.",
          file=out, flush=True)
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        ui.close()
        worker.join(10)
    finally:
        httpd.server_close()
        shutil.rmtree(ui.pages_dir, ignore_errors=True)
    return result["rc"]
