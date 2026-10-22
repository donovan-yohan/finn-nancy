"""Private atomic files with opaque SQLite references, never plaintext DB tokens."""
from __future__ import annotations

import os
import re
import secrets
import stat
from pathlib import Path

from .client import BankingError


def check_private_path(path: Path) -> None:
    if not path.is_absolute():
        raise BankingError('unsafe_private_path')
    # Reject symlinks throughout the configured path, not just its final component.
    for part in (path, *path.parents):
        if part.is_symlink():
            raise BankingError('unsafe_private_path')
        # Inspect markers only: never invoke Git or read repository metadata.
        # A .git file also marks a linked worktree; ignored paths remain unsafe.
        marker = part / '.git'
        if (marker.exists() or marker.is_symlink() or
                ((part / 'HEAD').is_file() and (part / 'objects').is_dir() and (part / 'refs').is_dir())):
            raise BankingError('private_path_inside_git')


def private_read(path: Path) -> str:
    path = Path(path)
    check_private_path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'r') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise BankingError('unsafe_private_permissions')
            value = handle.read(65537)
            if len(value) > 65536:
                raise BankingError('invalid_private_value')
            return value
    except OSError:
        raise BankingError('private_store_unavailable') from None


class PrivateStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        check_private_path(self.directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._check()

    def _check(self):
        check_private_path(self.directory)
        info = self.directory.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise BankingError('unsafe_private_permissions')

    def _path(self, ref: str) -> Path:
        if not isinstance(ref, str) or not re.fullmatch(r'[a-f0-9]{48}', ref):
            raise BankingError('invalid_private_reference')
        self._check()
        return self.directory / ref

    def put(self, value: str) -> str:
        if not isinstance(value, str) or not value or len(value) > 65536:
            raise BankingError('invalid_private_value')
        ref = secrets.token_hex(24)
        target = self._path(ref)
        temporary = self.directory / ('.pending-' + secrets.token_hex(24))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'w') as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            parent = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            temporary.unlink(missing_ok=True)
        return ref

    def get(self, ref: str) -> str:
        return private_read(self._path(ref))

    def remove(self, ref: str) -> None:
        path = self._path(ref)
        private_read(path)  # refuse replacing/removing an unsafe entry
        path.unlink()
