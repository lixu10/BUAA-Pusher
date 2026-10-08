from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("PUAA_HOST", "0.0.0.0")
    port: int = int(os.getenv("PUAA_PORT", "8000"))
    database_path: Path = Path(os.getenv("PUAA_DATABASE", ROOT / "data" / "puaa.db"))
    session_days: int = max(1, int(os.getenv("PUAA_SESSION_DAYS", "30")))
    trust_env_proxy: bool = _bool("PUAA_TRUST_ENV_PROXY", False)
    secure_cookies: bool = _bool("PUAA_SECURE_COOKIES", False)
    master_key: str = os.getenv("PUAA_MASTER_KEY", "")
    sync_interval_seconds: int = max(60, int(os.getenv("PUAA_SYNC_INTERVAL_SECONDS", "900")))


settings = Settings()
