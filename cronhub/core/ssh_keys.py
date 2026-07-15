import os
import subprocess
from pathlib import Path

SSH_DIR = Path(os.getenv("CRONHUB_SSH_DIR", "data/.ssh"))
KEY_NAME = os.getenv("CRONHUB_SSH_KEY_NAME", "id_ed25519")
KEY_COMMENT = os.getenv("CRONHUB_SSH_KEY_COMMENT", "cronhub-ansible")
SSH_USER = os.getenv("CRONHUB_SSH_USER", "ansible")

HOME_SSH_DIR = Path.home() / ".ssh"


def private_key_path() -> Path:
    return SSH_DIR / KEY_NAME


def public_key_path() -> Path:
    return SSH_DIR / f"{KEY_NAME}.pub"


def known_hosts_path() -> Path:
    return SSH_DIR / "known_hosts"


def _ensure_home_ssh_setup():
    """ssh/ssh-copy-id/scp need ~/.ssh to exist (scratch dir, default config
    and known_hosts location). ~/.ssh isn't in the persisted data volume, so
    it has to be recreated on every container start - this also points the
    default identity at the persisted key so jobs can just run
    `ssh user@host "cmd"` without -i, and sets StrictHostKeyChecking to
    accept-new so a first-time connection from a non-interactive job doesn't
    hang forever waiting on a yes/no prompt."""
    HOME_SSH_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(HOME_SSH_DIR, 0o700)

    kh = known_hosts_path()
    kh.touch(exist_ok=True)
    os.chmod(kh, 0o600)

    config_path = HOME_SSH_DIR / "config"
    config_path.write_text(
        "Host *\n"
        f"    IdentityFile {private_key_path().resolve()}\n"
        f"    UserKnownHostsFile {kh.resolve()}\n"
        "    StrictHostKeyChecking accept-new\n",
        encoding="utf-8",
    )
    os.chmod(config_path, 0o600)


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

    _ensure_home_ssh_setup()
    return priv


def read_public_key() -> str:
    ensure_ssh_key()
    return public_key_path().read_text(encoding="utf-8").strip()
