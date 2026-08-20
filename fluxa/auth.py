from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


AUTH_VERSION = 1
COOKIE_NAME = "__Secure-FluxaSession"
SESSION_SECONDS = 30 * 24 * 60 * 60
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 64 * 1024 * 1024


class AuthenticationError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuthConfig:
    salt: bytes
    password_hash: bytes
    session_secret: bytes


class AuthManager:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.path = data_dir / "auth.json"
        self.initial_password_path = data_dir / "initial-password.txt"

    @property
    def configured(self) -> bool:
        return self.path.is_file()

    def initialize(self) -> Path:
        if self.configured:
            raise AuthenticationError(f"Authentication is already configured in {self.path}")
        password = secrets.token_urlsafe(24)
        self.set_password(password)
        self._write_private(
            self.initial_password_path,
            (
                "Fluxa temporary household password\n"
                "\n"
                f"{password}\n"
                "\n"
                "Change it with: python3 server.py auth-set-password\n"
            ).encode("utf-8"),
        )
        return self.initial_password_path

    def set_password(self, password: str) -> None:
        if len(password) < 12:
            raise AuthenticationError("The household password must contain at least 12 characters")
        salt = secrets.token_bytes(16)
        digest = self._password_digest(password, salt)
        payload = {
            "version": AUTH_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "scrypt": {"n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P},
            "salt": _encode(salt),
            "password_hash": _encode(digest),
            "session_secret": _encode(secrets.token_bytes(32)),
        }
        self._write_private(
            self.path,
            (json.dumps(payload, indent=2) + "\n").encode("utf-8"),
        )
        try:
            self.initial_password_path.unlink()
        except FileNotFoundError:
            pass

    def verify_password(self, password: str) -> bool:
        config = self._load()
        candidate = self._password_digest(password, config.salt)
        return hmac.compare_digest(candidate, config.password_hash)

    def issue_session(self, now: int | None = None) -> str:
        config = self._load()
        issued_at = int(time.time() if now is None else now)
        payload = f"{issued_at + SESSION_SECONDS}:{secrets.token_urlsafe(18)}".encode()
        signature = hmac.new(config.session_secret, payload, hashlib.sha256).digest()
        return f"{_encode(payload)}.{_encode(signature)}"

    def verify_session(self, token: str, now: int | None = None) -> bool:
        try:
            payload_text, signature_text = token.split(".", 1)
            payload = _decode(payload_text)
            supplied_signature = _decode(signature_text)
            expires_text, _nonce = payload.decode("ascii").split(":", 1)
            expires = int(expires_text)
            config = self._load()
        except (AuthenticationError, UnicodeDecodeError, ValueError):
            return False
        expected = hmac.new(config.session_secret, payload, hashlib.sha256).digest()
        current = int(time.time() if now is None else now)
        return expires >= current and hmac.compare_digest(expected, supplied_signature)

    def _load(self) -> AuthConfig:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if int(payload["version"]) != AUTH_VERSION:
                raise AuthenticationError("Unsupported Fluxa authentication version")
            return AuthConfig(
                salt=_decode(payload["salt"]),
                password_hash=_decode(payload["password_hash"]),
                session_secret=_decode(payload["session_secret"]),
            )
        except FileNotFoundError as exc:
            raise AuthenticationError("Fluxa public authentication is not configured") from exc
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AuthenticationError(f"Invalid Fluxa authentication file: {self.path}") from exc

    @staticmethod
    def _password_digest(password: str, salt: bytes) -> bytes:
        return hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            maxmem=SCRYPT_MAXMEM,
            dklen=32,
        )

    def _write_private(self, path: Path, payload: bytes) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.data_dir, prefix=f".{path.name}.", text=False
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
