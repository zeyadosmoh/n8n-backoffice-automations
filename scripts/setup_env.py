"""Create .env from .env.example with random secrets. Refuses to overwrite an existing .env."""

import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPLACEMENTS = {
    "POSTGRES_PASSWORD": lambda: secrets.token_urlsafe(24),
    "STORE_API_TOKEN": lambda: "shpat_" + secrets.token_hex(16),
    "MOCK_ADMIN_TOKEN": lambda: secrets.token_urlsafe(24),
    "N8N_WEBHOOK_TOKEN": lambda: secrets.token_urlsafe(32),
}


def render(example: str) -> str:
    lines = []
    for line in example.splitlines():
        key = line.split("=", 1)[0]
        lines.append(f"{key}={REPLACEMENTS[key]()}" if key in REPLACEMENTS else line)
    return "\n".join(lines) + "\n"


def main() -> int:
    env = ROOT / ".env"
    if env.exists():
        print(".env already exists; leaving it untouched.")
        return 0
    env.write_text(render((ROOT / ".env.example").read_text()))
    env.chmod(0o600)
    print("Wrote .env with random secrets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
