from __future__ import annotations

import base64
import ipaddress
import os
import secrets
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import tomlkit
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, PrivateAttr, field_validator

HOST_COMMIT = "c90ec6478ad3e808ef22ba9464f8ad05054dde7b"
HOST_VERSION = "5.2.6"


class CatalogConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9]{0,31}$")
    region: str = Field(default="", pattern=r"^[a-z0-9]{0,16}$")
    client_artwork: bool = True

    @field_validator("key")
    @classmethod
    def valid_key(cls, value: str | None) -> str | None:
        if value in ("demo", "none") or (value and value.startswith("addon")):
            raise ValueError("reserved catalog key")
        return value


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    library: Path
    state_dir: Path = Path("state")
    host: str = "127.0.0.1"
    port: int = Field(default=4759, ge=1, le=65535)
    management_host: str | None = None
    management_port: int = Field(default=4760, ge=1, le=65535)
    management_public_url: str = ""
    management_trusted_proxies: list[str] = Field(default_factory=list)
    public_url: str = ""
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    call_timeout: float = Field(default=135, gt=0)
    download_stall_timeout: float = Field(default=180, gt=0)
    download_timeout: float = Field(default=14400, gt=0)
    cancel_timeout: float = Field(default=10, gt=0)
    shutdown_timeout: float = Field(default=15, gt=0)
    download_backoff: float = Field(default=300, gt=0)
    admission: int = Field(default=8, ge=1, le=64)
    preparations: int = Field(default=6, ge=1, le=16)
    stream_wait: float = Field(default=8, ge=0)
    cache_bytes: int = Field(default=2 * 1024**3, gt=0)
    cache_age: float = Field(default=7 * 86400, gt=0)
    registries: list[str] | None = None
    _path: Path | None = PrivateAttr(default=None)
    catalogs: dict[str, CatalogConfig] = Field(default_factory=dict)

    @field_validator("management_public_url")
    @classmethod
    def valid_management_origin(cls, value: str) -> str:
        if not value:
            return value
        parsed = AnyHttpUrl(value)
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or "?" in value
            or "#" in value
            or "\\" in value
            or parsed.port == 0
            or any(character.isspace() or ord(character) < 32 for character in value)
        ):
            raise ValueError(
                "management_public_url must be an HTTP(S) origin without a path prefix"
            )
        return str(parsed).rstrip("/")

    @field_validator("management_trusted_proxies")
    @classmethod
    def valid_management_proxies(cls, values: list[str]) -> list[str]:
        # Literal addresses only: no wildcard, hostname or implicit environment trust.
        return [str(ipaddress.ip_address(value)) for value in values]

    @classmethod
    def load(cls, path: Path) -> Config:
        data = tomllib.loads(path.read_text())
        # Paths in TOML are relative to that TOML, never the caller's working directory.
        for key in ("library", "state_dir"):
            p = Path(data.get(key, "state")).expanduser()
            data[key] = p if p.is_absolute() else path.resolve().parent / p
        config = cls.model_validate(data)
        overrides = config.state_dir / "settings.toml"
        if overrides.exists():
            saved = tomllib.loads(overrides.read_text())
            if set(saved) - {"registries", "catalogs"}:
                raise ValueError("unsupported saved setting")
            config = cls.model_validate(config.model_dump() | saved)
        config._path = path.resolve()
        return config

    def persist(self, **changes: Any) -> None:
        # Page state overlays declarative configuration (which may live in the Nix store).
        if set(changes) - {"registries", "catalogs"}:
            raise ValueError("unsupported saved setting")
        validated = self.model_validate(self.model_dump() | changes)
        path = self.state_dir / "settings.toml"
        temporary: str | None = None
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            document = tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()
            for key, value in changes.items():
                document[key] = value
            fd, temporary = tempfile.mkstemp(dir=self.state_dir, prefix=".bridge-settings-")
            with os.fdopen(fd, "w") as stream:
                stream.write(tomlkit.dumps(document))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError:
            from .errors import BridgeError

            raise BridgeError(
                "settings_save_failed_check_state_directory_permissions", 503
            ) from None
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)
        for key in changes:
            setattr(self, key, getattr(validated, key))

    def initialize(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_dir, 0o700)
        for name in ("support", "documents", "temp", "extensions", "data", "repo", "jobs", "audio"):
            (self.state_dir / name).mkdir(exist_ok=True, mode=0o700)
        secret_file(
            self.state_dir / "master.key", base64.b64encode(secrets.token_bytes(32)).decode()
        )
        secret_file(self.state_dir / "access.token", secrets.token_urlsafe(32))
        if len(base64.b64decode((self.state_dir / "master.key").read_text(), validate=True)) != 32:
            raise ValueError("invalid master key")

    def worker_config(self) -> dict[str, Any]:
        return {
            "library": str(self.library),
            "root": str(self.state_dir.resolve()),
            "ffmpeg": self.ffmpeg,
            "ffprobe": self.ffprobe,
        }


def secret_file(path: Path, initial: str) -> str:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        os.chmod(path, 0o600)
    else:
        with os.fdopen(fd, "w") as f:
            f.write(initial)
            f.flush()
            os.fsync(f.fileno())
    return path.read_text().strip()
