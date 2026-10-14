"""Atomic single-key update of a dotenv file, keeping its mode (V1-23)."""

import os
import re
import tempfile
from pathlib import Path


def set_env_value(path: Path, name: str, value: str) -> None:
    """Set ``name=value``: replace the first live or commented line for it, else append."""
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    eol = "\r\n" if "\r\n" in text else "\n"
    parts = text.split("\n")  # split on "\n" only; a CRLF line keeps its "\r"
    new = f"{name}={value}"
    live = re.compile(rf"^\s*{re.escape(name)}=")
    commented = re.compile(rf"^\s*#\s*{re.escape(name)}=")
    for pattern in (live, commented):  # a live line wins over a commented one
        idx = next((i for i, line in enumerate(parts) if pattern.match(line)), None)
        if idx is not None:
            parts[idx] = new + ("\r" if parts[idx].endswith("\r") else "")
            out = "\n".join(parts)
            break
    else:
        out = text if text.endswith("\n") or not text else text + eol
        out += new + eol
    mode = os.stat(path).st_mode & 0o777
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(out)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
