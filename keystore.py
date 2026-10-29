"""Local secret storage for the user's own API keys (local app mode).

Keys stay on the user's device: the OS keyring (macOS Keychain, Windows Credential Manager, Secret Service on
Linux) when one is usable, otherwise a JSON file readable only by the user (permission 600) in the user's
config directory. Keys are only ever sent to the provider's own API (api.opensea.io). They are never logged,
never put in reports, and the UI only receives a masked form ("••••abcd").
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

SERVICE = "mintscope"
KNOWN_KEYS = ("opensea_api_key",)
KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{16,128}$")


def config_dir() -> Path:
    override = os.environ.get("MINTSCOPE_CONFIG_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "MintScope"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MintScope"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "mintscope"


def mask(value: str | None) -> str | None:
    if not value:
        return None
    return "••••" + value[-4:] if len(value) > 8 else "••••"


def _usable_keyring():
    if os.environ.get("MINTSCOPE_KEYSTORE", "").lower() == "file":
        return None
    try:
        import keyring
        from keyring.backends import fail
        kr = keyring.get_keyring()
    except Exception:
        return None
    name = type(kr).__module__ + "." + type(kr).__name__
    if isinstance(kr, fail.Keyring) or "null" in name.lower() or getattr(kr, "priority", 0) <= 0:
        return None
    if type(kr).__name__ == "ChainerBackend" and not getattr(kr, "backends", None):
        return None
    return kr


class KeyStore:
    def __init__(self, directory: Path | None = None):
        self.dir = Path(directory) if directory else config_dir()
        self.file = self.dir / "secrets.json"
        self._kr = _usable_keyring()

    @property
    def backend(self) -> str:
        return "os-keyring" if self._kr else "file-600"

    # -- file backend ------------------------------------------------------------
    def _read_file(self) -> dict:
        try:
            data = json.loads(self.file.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_file(self, data: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass
        tmp = self.file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.file)

    # -- public API ----------------------------------------------------------------
    def get(self, name: str) -> str | None:
        if name not in KNOWN_KEYS:
            raise KeyError(name)
        if self._kr:
            try:
                value = self._kr.get_password(SERVICE, name)
                if value:
                    return value
            except Exception:
                pass
        return self._read_file().get(name) or None

    def set(self, name: str, value: str) -> None:
        if name not in KNOWN_KEYS:
            raise KeyError(name)
        value = (value or "").strip()
        if not KEY_RE.match(value):
            raise ValueError("The key format looks wrong (expected 16-128 letters, digits, '-' or '_').")
        if self._kr:
            try:
                self._kr.set_password(SERVICE, name, value)
                return
            except Exception:
                self._kr = None  # keyring locked or unavailable: fall back to the 600 file
        data = self._read_file()
        data[name] = value
        self._write_file(data)

    def delete(self, name: str) -> None:
        if name not in KNOWN_KEYS:
            raise KeyError(name)
        if self._kr:
            try:
                self._kr.delete_password(SERVICE, name)
            except Exception:
                pass
        data = self._read_file()
        if name in data:
            data.pop(name)
            self._write_file(data)

    def status(self, name: str) -> dict:
        value = self.get(name)
        return {"set": bool(value), "masked": mask(value), "storage": self.backend}
