#!/usr/bin/env python3
"""An inline prompt: an optional text field and a row of buttons.

Tab / Shift-Tab and the arrow keys move the highlight, Enter takes it, Esc
stops. Drawn in place below the text above it, so the scrollback keeps a
plain record of every answer. Only for a real terminal: `usable()` says when.
"""
import codecs
import os
import select
import shutil
import sys
import termios
import tty

REVERSE, UNDERLINE, DIM, RESET = "\033[7m", "\033[4m", "\033[2m", "\033[0m"
FIELD_MIN = 18


def usable():
    return (sys.stdin.isatty() and sys.stdout.isatty()
            and os.environ.get("TERM", "dumb") != "dumb")


class Stop(Exception):
    """Esc or Ctrl-D."""


def prompt(label, buttons=(), default="", text=False, actions=(), indent=4):
    """-> the value of the chosen button, or the typed text (empty -> default).

    `buttons` and `actions` are [(value, label)]; actions are drawn after a
    gap (datasheet, skip, stop). In a prompt without a text field, typing a
    button's value picks it, so the old one-letter answers still work.
    """
    return _Prompt(label, list(buttons), default, text, list(actions),
                   indent).run()


class _Prompt:
    def __init__(self, label, buttons, default, text, actions, indent):
        self.label, self.default, self.text = label, default, text
        self.buttons, self.actions = buttons, actions
        self.indent = indent
        self.buf, self.pos = "", 0
        self.targets = ([None] if text else []) + buttons + actions
        values = [b[0] for b in buttons]
        self.focus = 0 if text else (values.index(default) if default in values
                                     else 0)
        self.drawn_rows, self.cursor_row = 0, 0

    # ---- drawing ----------------------------------------------------------
    def segments(self):
        """-> [(plain text, styled text, is_field)] in reading order."""
        segs = [(self.label, self.label, False)]
        first_action = len(self.buttons) + (1 if self.text else 0)
        for i, t in enumerate(self.targets):
            if i == first_action and self.buttons and self.actions:
                segs.append(("·", DIM + "·" + RESET, False))
            if t is None:
                shown = self.buf or self.default
                width = max(FIELD_MIN, len(shown) + 1)
                pad = " " * (width - len(shown))
                style = UNDERLINE if self.focus == 0 else ""
                body = shown if self.buf else DIM + shown + RESET + style
                segs.append(("[" + shown + pad + "]",
                             "[" + style + body + pad + RESET + "]", True))
            elif i == self.focus:
                segs.append((f" {t[1]} ", REVERSE + f" {t[1]} " + RESET, False))
            else:
                segs.append((f" {t[1]} ", f" {t[1]} ", False))
        return segs

    def layout(self):
        """Greedy wrap. -> rows of styled text, and (row, col) of the cursor
        when the text field has the focus."""
        cols = max(40, shutil.get_terminal_size((88, 24)).columns - 1)
        rows, cursor = [], None
        row, width, empty = " " * self.indent, self.indent, True
        for plain, styled, is_field in self.segments():
            if not empty and width + 2 + len(plain) > cols:
                rows.append(row)
                row, width, empty = " " * (self.indent + 2), self.indent + 2, True
            if not empty:
                row, width = row + "  ", width + 2
            if is_field and self.focus == 0:
                cursor = (len(rows), width + 1 + self.pos)
            row, width, empty = row + styled, width + len(plain), False
        rows.append(row)
        return rows, cursor

    def draw(self, final=None):
        """Redraw in place. `final` replaces the prompt with a one-line record
        of the answer; "" erases it (the datasheet button asks again)."""
        out = []
        if self.cursor_row:
            out.append(f"\033[{self.cursor_row}A")
        out.append("\r\033[J")
        if final is not None:
            if final:
                out.append(" " * self.indent + f"{self.label}: {final}\n")
            out.append("\033[?25h")
            sys.stdout.write("".join(out))
            sys.stdout.flush()
            return
        rows, cursor = self.layout()
        out.append("\n".join(rows))
        self.drawn_rows = len(rows)
        if cursor:
            r, c = cursor
            up = len(rows) - 1 - r
            out.append((f"\033[{up}A" if up else "") + f"\r\033[{c}C\033[?25h")
            self.cursor_row = r
        else:
            out.append("\033[?25l")
            self.cursor_row = len(rows) - 1
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    # ---- keys -------------------------------------------------------------
    def run(self):
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            self.draw()
            for key in _keys(fd):
                done = self.handle(key)
                if done is not None:
                    shown = next((lbl for v, lbl in self.targets[1 if self.text
                                                                 else 0:]
                                  if v == done), done)
                    self.draw(final="" if done == ":o" else shown or "-")
                    return done
                self.draw()
        except Stop:
            self.draw(final="(stopped)")
            raise
        except KeyboardInterrupt:
            self.draw(final="(stopped)")
            raise
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)

    def handle(self, key):
        """-> the answer when the prompt is done, else None."""
        n = len(self.targets)
        in_field = self.text and self.focus == 0
        if key == "esc":
            raise Stop()
        if key == "\x04" and not self.buf:
            raise Stop()
        if key in ("\t", "down"):
            self.focus = (self.focus + 1) % n
        elif key in ("shift-tab", "up"):
            self.focus = (self.focus - 1) % n
        elif key == "enter":
            if in_field:
                return self.buf.strip() or self.default
            return self.targets[self.focus][0]
        elif key == "right":
            if in_field and self.pos < len(self.buf):
                self.pos += 1
            else:
                self.focus = min(self.focus + 1, n - 1)
        elif key == "left":
            if in_field:
                self.pos = max(0, self.pos - 1)
            else:
                self.focus = max(self.focus - 1, 0)
        elif in_field:
            self.edit(key)
        elif len(key) == 1:
            for value, _label in self.buttons:
                if key == value:
                    return value
            hot = {"o": ":o", "s": ":s", "q": ":q", "b": ":b"}
            if key in hot and any(v == hot[key] for v, _l in self.actions):
                return hot[key]
        return None

    def edit(self, key):
        if key == "backspace" and self.pos:
            self.buf = self.buf[:self.pos - 1] + self.buf[self.pos:]
            self.pos -= 1
        elif key == "delete":
            self.buf = self.buf[:self.pos] + self.buf[self.pos + 1:]
        elif key == "home":
            self.pos = 0
        elif key == "end":
            self.pos = len(self.buf)
        elif key == "\x15":   # Ctrl-U
            self.buf, self.pos = "", 0
        elif len(key) == 1 and key.isprintable():
            self.buf = self.buf[:self.pos] + key + self.buf[self.pos:]
            self.pos += 1


_CSI = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home",
        "F": "end", "Z": "shift-tab", "1~": "home", "4~": "end",
        "7~": "home", "8~": "end", "3~": "delete"}


def _keys(fd):
    """Yield key names, or single characters. A lone ESC is told from the
    start of an arrow key by waiting 50 ms for more bytes."""
    dec = codecs.getincrementaldecoder("utf-8")("replace")
    pending = ""
    while True:
        if not pending:
            pending = dec.decode(os.read(fd, 1024))
            continue
        ch, pending = pending[0], pending[1:]
        if ch == "\x1b":
            if not pending and select.select([fd], [], [], 0.05)[0]:
                pending = dec.decode(os.read(fd, 1024))
            if not pending:
                yield "esc"
                continue
            if pending[0] in "[O":
                pending = pending[1:]
                seq = ""
                while pending and not (pending[0].isalpha() or pending[0] == "~"):
                    seq, pending = seq + pending[0], pending[1:]
                if pending:
                    seq, pending = seq + pending[0], pending[1:]
                name = _CSI.get(seq)
                if name:
                    yield name
                continue
            yield "esc"
        elif ch in "\r\n":
            yield "enter"
        elif ch in "\x7f\x08":
            yield "backspace"
        else:
            yield ch
