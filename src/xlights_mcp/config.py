"""Configuration management for xLights MCP Server."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr
from pydantic_settings import BaseSettings


CONFIG_DIR = Path.home() / ".xlights-mcp"
CONFIG_FILE = CONFIG_DIR / "config.json"
AUDIO_CACHE_DIR = CONFIG_DIR / "audio_cache"


def _read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines; '#' comments and blank lines skipped."""
    values: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.lstrip().startswith("#"):
                values[key.strip()] = value.strip()
    return values


class FPPConfig(BaseModel):
    """Falcon Pi Player connection settings.

    `host` is the default FPP. `targets` names other FPPs (name -> host/IP) so tools can take
    fpp="radio". Logins for password-protected FPPs come from `env_file` (or $XLIGHTS_FPP_ENV):
    FPP_USER (default "admin") and FPP_PASS_<NAME> per target name. Passwords are read at call
    time, kept in a private attribute, sent only as an HTTP Basic auth header, and never stored
    in config.json or returned by any tool.
    """

    host: str = "fpp.local"
    port: int = 80
    timeout: float = 10.0
    targets: dict[str, str] = Field(default_factory=dict)
    env_file: str = ""
    name: str = ""  # target name this config was resolved for ("" = default host)

    _password: str | None = PrivateAttr(default=None)
    _user: str = PrivateAttr(default="admin")

    def resolve(self, fpp: str | None = None) -> "FPPConfig":
        """Return a copy aimed at one FPP: a target name, a raw host/IP, or None for `host`.

        Raises ValueError for an unknown name that doesn't look like a host.
        """
        key = (fpp or "").strip()
        if not key:
            name, host = "", self.host
        elif key.lower() in {k.lower() for k in self.targets}:
            name = next(k for k in self.targets if k.lower() == key.lower())
            host = self.targets[name]
        elif "." in key or ":" in key:
            name = next((k for k, v in self.targets.items() if v == key), "")
            host = key
        else:
            known = ", ".join(sorted(self.targets)) or "none configured"
            raise ValueError(f"Unknown FPP '{fpp}'. Known targets: {known} (or pass an IP/host).")
        out = self.model_copy(update={"host": host, "name": name})
        env_path = os.environ.get("XLIGHTS_FPP_ENV") or self.env_file
        env = _read_env_file(Path(env_path).expanduser()) if env_path else {}
        out._user = env.get("FPP_USER") or "admin"
        out._password = env.get(f"FPP_PASS_{name.upper()}") if name else None
        return out

    def auth(self):
        """httpx auth for this FPP, or None when no password is configured."""
        if not self._password:
            return None
        import httpx

        return httpx.BasicAuth(self._user, self._password)

    @property
    def label(self) -> str:
        return f"{self.name} ({self.host})" if self.name else self.host


class AudioConfig(BaseModel):
    """Audio analysis settings."""

    cache_dir: Path = AUDIO_CACHE_DIR
    demucs_model: str = "htdemucs"
    sample_rate: int = 22050
    frame_rate_ms: int = 25  # xLights standard: 25ms per frame (40fps)
    stem_separation_timeout_s: float = 180.0
    whisper_model: str = "small"  # "tiny"/"base" are fast but noticeably less accurate


class ServerConfig(BaseModel):
    """Main server configuration."""

    show_folders: dict[str, str] = Field(default_factory=dict)
    active_show: str = ""
    detected_folders: dict[str, str] = Field(default_factory=dict)
    fpp: FPPConfig = Field(default_factory=FPPConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    wiki_path: str = ""  # local clone of xLightsSequencer/xLights.wiki

    @property
    def active_show_path(self) -> Path | None:
        """Get the filesystem path for the currently active show."""
        folder = self.show_folders.get(self.active_show)
        if folder:
            return Path(folder).expanduser()
        return None

    def get_show_path(self, show_name: str) -> Path | None:
        """Get the filesystem path for a named show."""
        folder = self.show_folders.get(show_name)
        if folder:
            return Path(folder).expanduser()
        return None

    def list_shows(self) -> list[str]:
        """List all configured show names."""
        return list(self.show_folders.keys())


def _find_xlights_show_folders() -> dict[str, str]:
    """Auto-detect xLights show folders from common locations."""
    candidates = [
        Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "xLights",  # macOS iCloud
        Path.home() / "Documents" / "xLights",  # macOS/Windows default
        Path.home() / "xLights",  # Linux / simple
        Path("/opt/xLights"),  # Linux system-wide
    ]

    folders: dict[str, str] = {}
    for base in candidates:
        if not base.exists():
            continue
        for child in sorted(base.iterdir()):
            if child.is_dir() and (child / "xlights_rgbeffects.xml").exists():
                folders[child.name.lower()] = str(child)

    return folders


def load_config() -> ServerConfig:
    """Load configuration from disk, auto-detecting show folders if needed."""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            data = json.load(f)
        config = ServerConfig(**data)
        # Re-detect if all configured paths are missing
        if config.show_folders and not any(
            Path(p).expanduser().exists() for p in config.show_folders.values()
        ):
            detected = _find_xlights_show_folders()
            if detected:
                config.detected_folders = detected
                config.show_folders = {}
                config.active_show = ""
                save_config(config)
        return config

    # First run — auto-detect show folders but don't commit them yet
    folders = _find_xlights_show_folders()
    config = ServerConfig(
        show_folders={},
        active_show="",
        detected_folders=folders,
    )

    save_config(config)
    return config


def save_config(config: ServerConfig) -> None:
    """Persist configuration to disk."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config.model_dump(mode="json"), f, indent=2, default=str)
