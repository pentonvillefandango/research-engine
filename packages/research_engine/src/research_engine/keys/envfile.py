"""Atomic single-key update of a dotenv file, keeping its mode (V1-23)."""

import os
import re
import tempfile
from pathlib import Path


def set_env_value(path: Path, name: str, value: str) -> None:
    """Set ``name=value``: replace the first live or commented line for it, else append."""
    text = path.read_text()
    lines = text.splitlines()
    pattern = re.compile(rf"^\s*(#\s*)?{re.escape(name)}=")
    new = f"{name}={value}"
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = new
            break
    else:
        lines.append(new)
    mode = os.stat(path).st_mode & 0o777
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
