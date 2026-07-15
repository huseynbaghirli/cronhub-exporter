import os
import subprocess
from pathlib import Path

SSH_DIR = Path(os.getenv("CRONHUB_SSH_DIR", "data/.ssh"))
KEY_NAME = os.getenv("CRONHUB_SSH_KEY_NAME", "id_ed25519")
KEY_COMMENT = os.getenv("CRONHUB_SSH_KEY_COMMENT", "cronhub-ansible")
SSH_USER = os.getenv("CRONHUB_SSH_USER", "ansible")


def private_key_path() -> Path:
    return SSH_DIR / KEY_NAME


def public_key_path() -> Path:
    return SSH_DIR / f"{KEY_NAME}.pub"


def ensure_ssh_key() -> Path:
    """Generates a persistent keypair on first run so shell jobs can ssh out
    without a password. Never overwrites an existing key."""
    SSH_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(SSH_DIR, 0o700)

    priv = private_key_path()
    if not priv.exists():
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(priv), "-C", KEY_COMMENT],
            check=True,
            capture_output=True,
            text=True,
        )
        os.chmod(priv, 0o600)
        os.chmod(public_key_path(), 0o644)
    return priv


def read_public_key() -> str:
    ensure_ssh_key()
    return public_key_path().read_text(encoding="utf-8").strip()
