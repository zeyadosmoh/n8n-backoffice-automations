"""The e2e stack: a throwaway docker compose project (local) or already-running service containers (CI).

Local (RUN_E2E=1): writes .e2e/.env with fresh random secrets, starts compose project "p8-e2e" on
127.0.0.1:5681 (n8n) / 5439 (Postgres) / 8781 (mock store) / 1027 + 8027 (Mailpit), migrates, seeds, imports and
publishes the workflows, and removes everything with `down -v` afterwards (E2E_KEEP=1 keeps it).

CI (E2E_EXTERNAL=1): the workflow job already started the services and imported the workflows; the same variable
names (POSTGRES_*, STORE_API_TOKEN, MOCK_ADMIN_TOKEN, N8N_WEBHOOK_TOKEN, *_PORT, EXPORTS_DIR, ...) come from the
environment.
"""

from __future__ import annotations

import io
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import psycopg
from psycopg.rows import dict_row

from p8.config import ROOT, load_env, pg_dsn
from p8.db import migrate, seed_products

E2E_DIR = ROOT / ".e2e"
PROJECT = "p8-e2e"
PORTS = {"POSTGRES_PORT": "5439", "N8N_PORT_HOST": "5681", "MOCK_STORE_PORT": "8781", "MAILPIT_SMTP_PORT": "1027",
         "MAILPIT_UI_PORT": "8027"}


def _clean_env() -> dict[str, str]:
    """Shell variables would override --env-file in compose interpolation, so pass only what docker needs."""
    keep = ("PATH", "HOME", "USER", "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT", "XDG_RUNTIME_DIR")
    return {k: v for k, v in os.environ.items() if k in keep}


def _compose(env_file: Path, *args: str, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "compose", "-p", PROJECT, "--env-file", str(env_file), *args], cwd=ROOT,
                          check=check, capture_output=True, text=True, env=_clean_env())


def write_env() -> Path:
    import importlib.util

    spec = importlib.util.spec_from_file_location("setup_env", ROOT / "scripts" / "setup_env.py")
    setup_env = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup_env)
    text = setup_env.render((ROOT / ".env.example").read_text())
    overrides = {
        **PORTS,
        "EXPORTS_DIR": str(E2E_DIR / "exports"),
        "N8N_EDITOR_BASE_URL": "http://localhost:5681",   # Change B: links follow this setting
        "AI_MATCH_ENABLED": "true",
        "GEMINI_API_BASE": "http://mock-store:8000/_fake_gemini/v1beta",
        "MOCK_FAIL_RATE": "0",
    }
    lines = []
    for line in text.splitlines():
        k = line.split("=", 1)[0]
        lines.append(f"{k}={overrides.pop(k)}" if k in overrides else line)
    lines += [f"{k}={v}" for k, v in overrides.items()]
    E2E_DIR.mkdir(exist_ok=True)
    (E2E_DIR / "exports").mkdir(exist_ok=True)
    env = E2E_DIR / ".env"
    env.write_text("\n".join(lines) + "\n")
    env.chmod(0o600)
    return env


@dataclass
class Stack:
    env: dict[str, str]
    exports_dir: Path
    env_file: Path | None = None

    # -- endpoints ----------------------------------------------------------------------------------
    @property
    def n8n(self) -> str:
        return f"http://127.0.0.1:{self.env['N8N_PORT_HOST']}"

    @property
    def mailpit(self) -> str:
        return f"http://127.0.0.1:{self.env['MAILPIT_UI_PORT']}"

    @property
    def mock(self) -> str:
        return f"http://127.0.0.1:{self.env['MOCK_STORE_PORT']}"

    @property
    def editor_base(self) -> str:
        return self.env.get("N8N_EDITOR_BASE_URL", "http://localhost:5680").rstrip("/")

    def db(self) -> psycopg.Connection:
        env = {**self.env, "POSTGRES_HOST": "127.0.0.1"}
        return psycopg.connect(pg_dsn(env), autocommit=True, row_factory=dict_row)

    def q(self, sql: str, params=None) -> list[dict]:
        with self.db() as c:
            return c.execute(sql, params).fetchall()

    def x(self, sql: str, params=None) -> None:
        with self.db() as c:
            c.execute(sql, params)

    # -- n8n ----------------------------------------------------------------------------------------
    def wait_ready(self, timeout: float = 120) -> None:
        """Every run-now webhook and the form must be registered (anything but 404) before tests start."""
        urls = [("POST", f"{self.n8n}/webhook/p8/{p}") for p in ("orders-sync", "orders-export", "low-stock")]
        urls.append(("GET", f"{self.n8n}/form/p8-price-list"))
        end = time.monotonic() + timeout
        last = None
        while time.monotonic() < end:
            try:
                last = [httpx.request(m, u, timeout=5).status_code for m, u in urls]
                if all(c != 404 for c in last):
                    return
            except httpx.HTTPError as exc:
                last = repr(exc)
            time.sleep(1)
        raise RuntimeError(f"n8n webhooks not registered after {timeout}s (last statuses: {last})")

    def run(self, path: str, body: dict | None = None, timeout: float = 300) -> httpx.Response:
        return httpx.post(f"{self.n8n}/webhook/{path}", json=body or {}, timeout=timeout,
                          headers={"X-P8-Token": self.env["N8N_WEBHOOK_TOKEN"]})

    def upload(self, path: Path) -> httpx.Response:
        with path.open("rb") as f:
            return httpx.post(f"{self.n8n}/form/p8-price-list", files={"field-0": (path.name, f)}, timeout=120)

    # -- mock store ---------------------------------------------------------------------------------
    def admin(self, what: str, body: dict | None = None) -> dict:
        h = {"X-Admin-Token": self.env["MOCK_ADMIN_TOKEN"]}
        if body is None:
            r = httpx.get(f"{self.mock}/_admin/{what}", headers=h, timeout=30)
        else:
            r = httpx.post(f"{self.mock}/_admin/{what}", json=body, headers=h, timeout=30)
        r.raise_for_status()
        return r.json()

    def calm(self) -> None:
        self.admin("faults", {"fail_rate": 0, "fail_next": 0, "bucket_size": 40, "leak_rps": 2})

    # -- Mailpit ------------------------------------------------------------------------------------
    def clear_mail(self) -> None:
        httpx.delete(f"{self.mailpit}/api/v1/messages", timeout=10)

    def messages(self) -> list[dict]:
        return httpx.get(f"{self.mailpit}/api/v1/messages", params={"limit": 200}, timeout=10).json()["messages"]

    def wait_mail(self, contains: str, timeout: float = 60) -> dict:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for m in self.messages():
                if contains in m["Subject"]:
                    return httpx.get(f"{self.mailpit}/api/v1/message/{m['ID']}", timeout=10).json()
            time.sleep(0.5)
        raise AssertionError(f"no email with {contains!r} within {timeout}s; got {[m['Subject'] for m in self.messages()]}")

    def attachment(self, msg: dict, prefix: str) -> str:
        a = next(a for a in msg["Attachments"] if a["FileName"].startswith(prefix))
        return httpx.get(f"{self.mailpit}/api/v1/message/{msg['ID']}/part/{a['PartID']}", timeout=10).text


def local_stack() -> Stack:
    env_file = write_env()
    _compose(env_file, "down", "-v", check=False)
    _compose(env_file, "up", "-d", "--build", "--wait")
    stack = Stack(env=load_env(env_file, use_environ=False), exports_dir=E2E_DIR / "exports", env_file=env_file)
    prepare_db(stack)
    subprocess.run(["scripts/import_workflows.sh"], cwd=ROOT, check=True, capture_output=True, text=True,
                   env={**_clean_env(), "ENV_FILE": str(env_file), "PROJECT": PROJECT})
    stack.wait_ready()
    return stack


def external_stack() -> Stack:
    env = dict(os.environ)
    stack = Stack(env=env, exports_dir=Path(env.get("EXPORTS_DIR", "/tmp/p8-exports")))
    prepare_db(stack)
    stack.wait_ready()
    return stack


def prepare_db(stack: Stack) -> None:
    for _ in range(60):
        try:
            with stack.db() as c:
                migrate(c)
                seed_products(c)
                return
        except psycopg.OperationalError:
            time.sleep(1)
    raise RuntimeError("Postgres for the e2e stack is not reachable")


def teardown(stack: Stack) -> None:
    if stack.env_file and os.environ.get("E2E_KEEP") != "1":
        _compose(stack.env_file, "down", "-v", check=False)


def xlsx_rows(path: Path) -> list[list]:
    from openpyxl import load_workbook

    ws = load_workbook(io.BytesIO(path.read_bytes())).active
    return [[c.value for c in row] for row in ws.iter_rows()]

