"""Files the server writes and the solver's files it reads, safe on a cloud-synced drive.

Writing: the bytes go to a temporary file on the local disk first, are copied next to
the target under a part name, checked there (length and SHA-256), renamed onto the
target and checked once more. A sync client that holds the file for a moment (a
PermissionError) is waited for; a copy that comes out different is made again. The
target is never left half written: it is either the old file or the new one.

Reading: a file of the solver (the progress file <n>.info.json, the run registry) can
carry bytes after its content - NUL padding of a preallocated file, the rest of an
older, longer version, a sync client's leftovers. read_json takes the first JSON value
and ignores what follows; read_text drops NUL bytes; read_csv_rows skips lines that are
not rows of the header's width.
"""

import csv
import hashlib
import io
import json
import os
import shutil
import tempfile
import time

ATTEMPTS = 4
BUSY_WAIT_S = (0.2, 0.5, 1.0, 2.0)


class SafeWriteError(OSError):
    pass


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_digest(path: str) -> tuple:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
    return size, h.hexdigest()


def _retry(action, what: str):
    """Run action(); a PermissionError (a sync client or a viewer holds the file) is waited for."""
    last = None
    for wait in (*BUSY_WAIT_S, None):
        try:
            return action()
        except PermissionError as exc:
            last = exc
            if wait is None:
                break
            time.sleep(wait)
    raise SafeWriteError(f"{what}: the file stays locked ({last.strerror or last}).") from None


def write_bytes(path: str, data: bytes) -> str:
    """Write data to path via a local temporary file, a copy beside the target and a rename;
    the copy and the target are checked by length and SHA-256. Returns the absolute path."""
    final = os.path.abspath(path)
    folder = os.path.dirname(final)
    os.makedirs(folder, exist_ok=True)
    want = (len(data), _digest(data))
    fd, local = tempfile.mkstemp(prefix="swmcp_", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if _file_digest(local) != want:
            raise SafeWriteError(f"The local temporary copy of '{os.path.basename(final)}' came out different.")
        part = f"{final}.{os.getpid()}.part"
        problem = "no attempt"
        for _ in range(ATTEMPTS):
            try:
                _retry(lambda: shutil.copyfile(local, part), f"Copying to '{part}'")
                if _file_digest(part) != want:
                    problem = "the copy beside the target differs in length or hash"
                    continue
                try:
                    _retry(lambda: os.replace(part, final), f"Renaming onto '{final}'")
                except SafeWriteError:
                    raise
                except OSError:
                    # a virtual drive that cannot rename over a file: copy onto it instead
                    _retry(lambda: shutil.copyfile(local, final), f"Copying onto '{final}'")
                if _file_digest(final) == want:
                    return final
                problem = "the target differs in length or hash after the rename"
            finally:
                if os.path.exists(part):
                    try:
                        os.remove(part)
                    except OSError:
                        pass
        raise SafeWriteError(f"'{final}' could not be written intact after {ATTEMPTS} attempts: {problem}.")
    finally:
        try:
            os.remove(local)
        except OSError:
            pass


def write_text(path: str, text: str, encoding: str = "utf-8") -> str:
    return write_bytes(path, text.encode(encoding))


def write_json(path: str, data, indent: int | None = 1, default=None) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=indent, default=default)
    return write_text(path, text + "\n")


def clean_bytes(data: bytes) -> bytes:
    """data without NUL bytes (padding of a preallocated or half-synced file)."""
    return data.replace(b"\x00", b"")


def read_text(path: str, encoding: str = "utf-8-sig") -> str:
    with open(path, "rb") as fh:
        data = fh.read()
    return clean_bytes(data).decode(encoding, errors="replace")


def parse_json_head(text: str):
    """The first JSON value of text; whatever follows it is ignored. ValueError when the
    text does not start with a complete JSON value (a file still being written)."""
    start = len(text) - len(text.lstrip(" \t\r\n﻿"))
    value, _ = json.JSONDecoder().raw_decode(text, start)
    return value


def read_json(path: str):
    """The first JSON value of a file, garbage after it ignored; OSError / ValueError as json.load."""
    return parse_json_head(read_text(path))


def csv_rows(text: str, delimiter: str = ";") -> tuple:
    """(header, [row dicts]) of a CSV text; a line whose number of fields is not the
    header's (a torn or padded tail, a merge leftover) is skipped."""
    lines = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    if not lines:
        return [], []
    header = lines[0]
    rows = [dict(zip(header, line)) for line in lines[1:] if len(line) == len(header)]
    return header, rows


def append_csv_row(path: str, columns, row: dict, delimiter: str = ";") -> str:
    """Add one row to a CSV (made with its header when new) and write the whole file back
    safely. A garbage tail of the old file (NULs, a torn last line) is dropped; the
    header of an existing file is kept and the row follows it."""
    columns = list(columns)
    header, rows = [], []
    if os.path.isfile(path):
        header, rows = csv_rows(read_text(path), delimiter)
    header = header + [c for c in columns if c not in header] if header else columns
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=header, delimiter=delimiter, extrasaction="ignore",
                            lineterminator="\r\n")
    writer.writeheader()
    for old in rows:
        writer.writerow(old)
    writer.writerow({k: "" if v is None else v for k, v in row.items()})
    return write_text(path, out.getvalue(), "utf-8-sig")
