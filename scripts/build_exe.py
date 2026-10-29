#!/usr/bin/env python3
"""Build a standalone MintScope executable for the current OS/CPU with PyInstaller.

    pip install -r requirements.txt pyinstaller
    python scripts/build_exe.py            ->  dist/mintscope-<os>-<arch>[.exe]

The admin license script, tests and any key files are NOT bundled.
"""
from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def target_name() -> str:
    os_name = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
    arch = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(arch, arch)
    return f"mintscope-{os_name}-{arch}"


def main() -> None:
    name = target_name()
    hidden = ["uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
              "uvicorn.protocols.http.h11_impl", "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
              "keyring.backends", "keyring.backends.macOS", "keyring.backends.Windows",
              "keyring.backends.SecretService", "keyring.backends.fail"]
    cmd = [sys.executable, "-m", "PyInstaller", "--onefile", "--noconfirm", "--clean", "--name", name,
           "--paths", str(ROOT), "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"),
           "--specpath", str(ROOT / "build"), "--exclude-module", "pytest", "--exclude-module", "tests",
           "--exclude-module", "Crypto", "--collect-submodules", "keyring"]
    for h in hidden:
        cmd += ["--hidden-import", h]
    cmd.append(str(ROOT / "cli.py"))
    print(" ".join(cmd))
    subprocess.run(cmd, check=True, cwd=ROOT)
    exe = ROOT / "dist" / (name + (".exe" if sys.platform == "win32" else ""))
    out = subprocess.run([str(exe), "version"], capture_output=True, text=True, check=True)
    print(f"built {exe} ({exe.stat().st_size // 1024} KB): {out.stdout.strip()}")


if __name__ == "__main__":
    main()
