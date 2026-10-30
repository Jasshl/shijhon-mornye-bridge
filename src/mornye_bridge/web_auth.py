"""Password storage and bounded, revocable server-side browser sessions."""

from __future__ import annotations

import asyncio
import os
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from .errors import BridgeError

HASHER = PasswordHasher()
COOKIE = "bridge_session"


def set_password(state_dir: Path, password: str, *, replace: bool = True) -> str:
    if not password or len(password) > 1024:
        raise BridgeError("password_must_be_nonempty_and_at_most_1024_characters", 400)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = state_dir / "password.argon2"
    temporary = state_dir / (".password-" + secrets.token_hex(12))
    encoded = HASHER.hash(password)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            # Atomic first claim: a concurrent CLI reset must never be overwritten.
            os.link(temporary, path)
        return encoded
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class Session:
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    authenticated: bool = False
    expires: float = field(default_factory=lambda: time.monotonic() + 600)
    password_hash: str = ""
    forms: dict[str, dict[str, Any]] = field(default_factory=dict)
    auth_urls: dict[str, tuple[float, str]] = field(default_factory=dict)
    retries: dict[str, tuple[float, str, dict[str, Any]]] = field(default_factory=dict)
    notice: str = ""


class BrowserAuth:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.sessions: dict[str, Session] = {}
        self.attempts: deque[tuple[float, str]] = deque(maxlen=100)
        self.login_lock = asyncio.Lock()

    def start(self) -> None:
        self.sessions.clear()
        self.attempts.clear()

    def password_hash(self) -> str:
        try:
            return (self.state_dir / "password.argon2").read_text().strip()
        except FileNotFoundError:
            return ""

    def session(self, token: str) -> Session | None:
        now = time.monotonic()
        current_hash = self.password_hash()
        self.sessions = {
            key: value
            for key, value in self.sessions.items()
            if value.expires > now
            and (not value.authenticated or value.password_hash == current_hash)
        }
        return self.sessions.get(token)

    def create(self, password_hash: str = "") -> tuple[str, Session]:
        self.session("")
        if len(self.sessions) >= 512:
            raise BridgeError("too_many_sessions", 429, 3)
        token = secrets.token_urlsafe(32)
        session = Session(
            authenticated=bool(password_hash),
            expires=time.monotonic() + (28800 if password_hash else 600),
            password_hash=password_hash,
        )
        self.sessions[token] = session
        return token, session

    def limit_attempt(self, address: str) -> None:
        now = time.monotonic()
        while self.attempts and self.attempts[0][0] < now - 60:
            self.attempts.popleft()
        if sum(ip == address for _, ip in self.attempts) >= 5 or len(self.attempts) >= 30:
            raise BridgeError("login_rate_limited", 429, 60)
        self.attempts.append((now, address))

    async def setup(self, password: str, address: str) -> str:
        async with self.login_lock:
            self.limit_attempt(address)
            if self.password_hash():
                return ""
            try:
                encoded = await asyncio.to_thread(
                    set_password, self.state_dir, password, replace=False
                )
            except FileExistsError:
                # Another password writer won the claim while Argon2 was running.
                return ""
            self.sessions.clear()
            return encoded

    async def verify(self, password: str, address: str) -> str:
        """Return the verified hash so a concurrent reset cannot authorize a new one."""
        async with self.login_lock:
            self.limit_attempt(address)
            encoded = self.password_hash()
            if not encoded or len(password) > 1024:
                return ""
            try:
                if await asyncio.to_thread(HASHER.verify, encoded, password):
                    return encoded
            except (VerificationError, InvalidHashError):
                pass
            return ""
