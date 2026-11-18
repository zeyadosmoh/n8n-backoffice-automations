"""Settings from the environment, with .env as a fallback (no extra dependency)."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path | None = None) -> dict[str, str]:
    """Values from .env (if present) overridden by the real environment."""
    values: dict[str, str] = {}
    path = path or ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    values.update({k: v for k, v in os.environ.items()})
    return values


def pg_dsn(env: dict[str, str] | None = None, dbname: str | None = None) -> str:
    env = env or load_env()
    return (
        f"host={env.get('POSTGRES_HOST', '127.0.0.1')} port={env.get('POSTGRES_PORT', '5438')} "
        f"user={env['POSTGRES_USER']} password={env['POSTGRES_PASSWORD']} "
        f"dbname={dbname or env.get('POSTGRES_DB', 'p8')}"
    )
