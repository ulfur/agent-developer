"""Utilities for generating and exposing SSH keys for Nightshift."""

from __future__ import annotations

import logging
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional


class SSHKeyError(RuntimeError):
    """Raised when SSH key generation or retrieval fails."""


class SSHKeyManager:
    """Ensures SSH key pairs exist and provides metadata for the public keys."""

    def __init__(self, data_dir: Path, logger: Optional[logging.Logger] = None) -> None:
        self.data_dir = data_dir
        self.keys_dir = self.data_dir / "ssh"
        self.keys_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logger or logging.getLogger(__name__)
        self._default_comment = f"nightshift@{socket.gethostname() or 'localhost'}"
        self._key_specs: List[Dict[str, Any]] = [
            {"type": "ed25519", "filename": "id_ed25519"},
        ]

    # ------------------------------------------------------------------
    def ensure_default_keys(self) -> List[Dict[str, str]]:
        """Ensure all default keys exist and return their metadata."""
        keys: List[Dict[str, str]] = []
        for spec in self._key_specs:
            keys.append(self._ensure_key_pair(spec))
        return keys

    def list_public_keys(self) -> List[Dict[str, str]]:
        """Return metadata for managed SSH public keys, ensuring they exist first."""
        return self.ensure_default_keys()

    # ------------------------------------------------------------------
    def _ensure_key_pair(self, spec: Dict[str, Any]) -> Dict[str, str]:
        key_type = spec["type"]
        filename = spec["filename"]
        private_path = self.keys_dir / filename
        public_path = self.keys_dir / f"{filename}.pub"

        if private_path.exists() and not public_path.exists():
            try:
                private_path.unlink()
            except OSError as exc:
                raise SSHKeyError(f"Unable to refresh {key_type} key: {exc}") from exc
        if public_path.exists() and not private_path.exists():
            try:
                public_path.unlink()
            except OSError as exc:
                raise SSHKeyError(f"Unable to refresh {key_type} key: {exc}") from exc

        if not private_path.exists() or not public_path.exists():
            self._generate_key_pair(key_type, private_path)

        public_key = self._read_public_key(public_path)
        fingerprint = self._read_fingerprint(public_path)
        self._sync_to_home_ssh(private_path, public_path)

        return {
            "type": key_type,
            "name": filename,
            "public_key": public_key,
            "fingerprint": fingerprint,
        }

    def _generate_key_pair(self, key_type: str, private_path: Path) -> None:
        comment = self._default_comment
        try:
            result = subprocess.run(
                [
                    "ssh-keygen",
                    "-q",
                    "-t",
                    key_type,
                    "-f",
                    str(private_path),
                    "-N",
                    "",
                    "-C",
                    comment,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
        except FileNotFoundError as exc:
            raise SSHKeyError("ssh-keygen is required to generate SSH keys") from exc
        except subprocess.CalledProcessError as exc:  # pragma: no cover - depends on host tooling
            stderr = (exc.stderr or "").strip() or (exc.stdout or "").strip()
            raise SSHKeyError(f"Failed to generate {key_type} key: {stderr}") from exc

        try:
            private_path.chmod(0o600)
            public_path = self.keys_dir / f"{private_path.name}.pub"
            public_path.chmod(0o644)
        except OSError:
            # Non-fatal: permissions best-effort on filesystems that support them.
            pass

        self.logger.info("Generated %s SSH key at %s", key_type, private_path)

    def _read_public_key(self, public_path: Path) -> str:
        try:
            content = public_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SSHKeyError(f"Unable to read public key {public_path.name}: {exc}") from exc
        if not content:
            raise SSHKeyError(f"Public key {public_path.name} is empty")
        return content

    def _read_fingerprint(self, public_path: Path) -> str:
        try:
            result = subprocess.run(
                ["ssh-keygen", "-lf", str(public_path)],
                capture_output=True,
                text=True,
                check=True,
            )
        except FileNotFoundError:
            return ""
        except subprocess.CalledProcessError:
            return ""
        line = (result.stdout or "").strip().splitlines()
        if not line:
            return ""
        parts = line[0].split()
        if len(parts) >= 2:
            return parts[1]
        return line[0]

    # ------------------------------------------------------------------
    def _sync_to_home_ssh(self, private_path: Path, public_path: Path) -> None:
        """Copy managed keys into ~/.ssh for interactive use."""

        home_dir = Path.home()
        ssh_dir = home_dir / ".ssh"
        try:
            ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            self.logger.warning("Unable to ensure ~/.ssh exists: %s", exc)
            return
        try:
            ssh_dir.chmod(0o700)
        except OSError:
            pass

        targets = [
            (private_path, ssh_dir / private_path.name, 0o600),
            (public_path, ssh_dir / f"{private_path.name}.pub", 0o644),
        ]
        for src, dst, mode in targets:
            try:
                shutil.copy2(src, dst)
                dst.chmod(mode)
            except OSError as exc:
                self.logger.warning("Unable to sync %s to %s: %s", src, dst, exc)
