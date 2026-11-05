"""Encryption at rest for stored document files (Fernet: AES-128-CBC + HMAC-SHA256).

The key comes from DOCAUTO_FILE_KEY (a Fernet key; several comma-separated keys rotate: the first encrypts, all
decrypt). Without it, a key is generated on first start into <data dir>/file.key: back that file up with the
data, or stored documents cannot be read again; better, keep the key in a secret store and set the variable.

Encrypted files start with MAGIC; files written before encryption existed have no header and are read as they are.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

log = logging.getLogger("docauto.filecrypt")
MAGIC = b"DFENC1\n"
_lock = threading.Lock()
_cache: dict[str, "FileCipher"] = {}


class FileCipher:
    def __init__(self, keys: list[bytes], source: str):
        if not keys:
            raise ValueError("no file encryption key")
        self.source = source
        self._f = MultiFernet([Fernet(k) for k in keys])

    def encrypt(self, data: bytes) -> bytes:
        return MAGIC + self._f.encrypt(data)

    def decrypt(self, blob: bytes) -> bytes:
        if not blob.startswith(MAGIC):
            return blob              # stored before encryption at rest: plain file
        try:
            return self._f.decrypt(blob[len(MAGIC):])
        except InvalidToken:
            raise ValueError(f"cannot decrypt a stored file with the key from {self.source} (wrong or missing key)")

    def write(self, path: Path, data: bytes) -> None:
        path.write_bytes(self.encrypt(data))

    def read(self, path: Path | str) -> bytes:
        return self.decrypt(Path(path).read_bytes())


def _key_file(path: Path) -> bytes:
    if path.exists():
        return path.read_bytes().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    try:   # O_EXCL: two processes starting together end up with the same key
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_bytes().strip()
    with os.fdopen(fd, "wb") as f:
        f.write(key + b"\n")
    log.warning("generated the file encryption key %s: back it up with the data (stored documents cannot be read "
                "without it), or move it to DOCAUTO_FILE_KEY", path)
    return key


def cipher(key_file: Path | str, env_key: Optional[str] = None) -> FileCipher:
    """The cipher for DOCAUTO_FILE_KEY (or env_key), else for the key in key_file (created when missing)."""
    raw = (env_key if env_key is not None else os.environ.get("DOCAUTO_FILE_KEY", "")).strip()
    cache_key = "env:" + raw if raw else "file:" + str(Path(key_file).resolve())
    with _lock:
        c = _cache.get(cache_key)
        if c is None:
            if raw:
                c = FileCipher([k.strip().encode() for k in raw.split(",") if k.strip()], "DOCAUTO_FILE_KEY")
            else:
                path = Path(key_file)
                c = FileCipher([_key_file(path)], str(path))
                log.info("stored files are encrypted with the key in %s", path)
            _cache[cache_key] = c
        return c
