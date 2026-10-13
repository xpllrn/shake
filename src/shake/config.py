import hashlib
import os
from pathlib import Path


def get_shake_home() -> Path:
    """Return the base shake configuration directory.

    Defaults to ~/.config/shake unless overridden by the SHAKE_HOME environment variable.
    """
    env_home = os.environ.get("SHAKE_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()
    return (Path.home() / ".config" / "shake").resolve()


class ShakeConfig:
    """Encapsulates system paths and secure permissions."""

    def __init__(self, home: Path | None = None) -> None:
        self.home: Path = home or get_shake_home()
        self.keys_dir: Path = self.home / "keys"
        self.db_path: Path = self.home / "shake.db"
        self.pid_path: Path = self.home / "shaked.pid"
        self.log_path: Path = self.home / "shaked.log"

    @property
    def socket_path(self) -> Path:
        """Return socket path, ensuring it stays under the 104-byte AF_UNIX limit on macOS."""
        default_path = self.home / "shaked.sock"
        if len(str(default_path)) > 95:
            short_hash = hashlib.sha256(str(self.home).encode("utf-8")).hexdigest()[:8]
            return Path(f"/tmp/shaked_{short_hash}.sock")
        return default_path

    def ensure_directories(self) -> None:
        """Create necessary directories with strict 0700 permissions."""
        for directory in (self.home, self.keys_dir):
            directory.mkdir(parents=True, exist_ok=True)
            # Ensure 0700 permissions on directory
            directory.chmod(0o700)

    def key_file_path(self, agent_id: str) -> Path:
        """Return the path to an agent's private key file."""
        return self.keys_dir / f"{agent_id}.key"

    @staticmethod
    def get_project_identity(cwd: Path | None = None) -> str | None:
        """Check for a per-project .shake/identity file in cwd or parents."""
        current = (cwd or Path.cwd()).resolve()
        identity_file = current / ".shake" / "identity"
        if identity_file.is_file():
            content = identity_file.read_text(encoding="utf-8").strip()
            if content:
                return content
        return None

    @staticmethod
    def set_project_identity(agent_id: str, cwd: Path | None = None) -> Path:
        """Write .shake/identity in the specified working directory."""
        current = (cwd or Path.cwd()).resolve()
        dot_shake = current / ".shake"
        dot_shake.mkdir(parents=True, exist_ok=True)
        dot_shake.chmod(0o700)
        identity_file = dot_shake / "identity"
        identity_file.write_text(f"{agent_id}\n", encoding="utf-8")
        identity_file.chmod(0o600)
        return identity_file
