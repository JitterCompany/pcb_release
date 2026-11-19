#!/usr/bin/env python3
"""Write a file so that an interrupted write leaves the old one intact.

Ctrl-C, a full disk or a crash between open() and close() would otherwise leave
a truncated table, CSV or schematic behind. Written next to the target and
renamed over it, which is atomic on one filesystem.
"""
import os
import tempfile


def write_text(path, text, encoding="utf-8"):
    write_bytes(path, text.encode(encoding))


def write_bytes(path, data):
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".bomcheck-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        else:
            # mkstemp makes 0600; a new file should get what open() would.
            mask = os.umask(0)
            os.umask(mask)
            os.chmod(tmp, 0o666 & ~mask)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
