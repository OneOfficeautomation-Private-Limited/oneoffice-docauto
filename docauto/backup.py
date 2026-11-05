"""Back up the DocFlow database (every tenant's schema) with pg_dump.

    python -m docauto.backup <data dir> <file.dump>

Restore with pg_restore (--clean --dbname <url>). Files under <data dir> (documents, test sets, file.key) are backed
up with the folder; windows/get-docflow.ps1 runs this before every update.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from .db import redact, resolve_url


def pg_dump_exe() -> str:
    """pg_dump: the embedded server's own, else the one on PATH."""
    import shutil
    try:
        from pgserver._commands import POSTGRES_BIN_PATH
        exe = Path(POSTGRES_BIN_PATH) / ("pg_dump.exe" if os.name == "nt" else "pg_dump")
        if exe.exists():
            return str(exe)
    except ImportError:
        pass
    found = shutil.which("pg_dump")
    if not found:
        raise SystemExit("pg_dump not found (install the PostgreSQL client tools)")
    return found


def main(argv: Optional[list[str]] = None) -> int:
    """python -m docauto.backup <data dir> <file.dump>"""
    import argparse
    import subprocess

    ap = argparse.ArgumentParser(prog="python -m docauto.backup", description=__doc__.splitlines()[0])
    ap.add_argument("data_dir", help="DocFlow data folder (its embedded server, unless DOCAUTO_DATABASE_URL is set)")
    ap.add_argument("out", help="backup file (pg_dump custom format)")
    a = ap.parse_args(argv)
    url = resolve_url(None, a.data_dir)
    r = subprocess.run([pg_dump_exe(), "--format=custom", "--file", a.out, "--dbname", url])
    if r.returncode == 0:
        print(f"database saved to {a.out} ({redact(url)})")
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
