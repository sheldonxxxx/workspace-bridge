"""Private single-admin credential storage, independent of MCP credentials."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import stat

from .security import BridgeError

BOOTSTRAP_PASSWORD = "admin"


def password_hash(password: str, salt: str) -> str:
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384,
                          r=8, p=1).hex()


class AdminAccount:
    def __init__(self, state: Path):
        self.state = state
        self.path = state / "admin-account.json"

    @contextmanager
    def locked(self):
        fd = os.open(self.state / "admin-account.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            self._private(fd)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    @staticmethod
    def _private(fd):
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077 or st.st_nlink != 1:
            raise BridgeError("Admin account files must be regular, private files (0600)")

    def read(self) -> dict:
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as source:
            self._private(source.fileno())
            account = json.load(source)
        if (not isinstance(account, dict) or account.get("username") != "admin"
                or not isinstance(account.get("must_change_password"), bool)
                or any(not isinstance(account.get(key), str)
                       or re.fullmatch(r"[0-9a-f]{" + str(length) + r"}", account[key]) is None
                       for key, length in (("salt", 32), ("password_hash", 128), ("revision", 32)))):
            raise BridgeError("Invalid admin account; use CLI password recovery")
        return account

    def _write(self, password: str, must_change: bool):
        salt = secrets.token_hex(16)
        account = {"username": "admin", "salt": salt, "password_hash": password_hash(password, salt),
                   "must_change_password": must_change, "revision": secrets.token_hex(16)}
        temporary = self.state / (".admin-account-" + secrets.token_hex(16))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w") as target:
                json.dump(account, target)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        return account

    def ensure(self):
        with self.locked():
            if not self.path.exists() and not self.path.is_symlink():
                return self._write(BOOTSTRAP_PASSWORD, True)
            return self.read()

    @staticmethod
    def verify(account: dict, password: str) -> bool:
        return secrets.compare_digest(account["password_hash"], password_hash(password, account["salt"]))

    def change(self, current: str, password: str, revision: str):
        if not isinstance(password, str) or not 8 <= len(password) <= 256:
            raise BridgeError("New password must contain 8 to 256 characters")
        if password == current or password == BOOTSTRAP_PASSWORD:
            raise BridgeError("Choose a different password")
        with self.locked():
            account = self.read()
            if account["revision"] != revision or not self.verify(account, current):
                raise BridgeError("Current password is incorrect")
            return self._write(password, False)

    def reset(self):
        with self.locked():
            # Refuse unsafe existing paths, including symlinks, before replacement.
            if self.path.exists() or self.path.is_symlink():
                fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    self._private(fd)
                finally:
                    os.close(fd)
            return self._write(BOOTSTRAP_PASSWORD, True)
