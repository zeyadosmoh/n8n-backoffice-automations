"""Fail if any exported workflow could leak a secret.

Checks every workflows/*.json:
  1. credentials are referenced by id + name only (no `data` / secret fields next to them);
  2. no value of a secret from .env (or the environment) appears anywhere in the file, including the run-now
     webhook token N8N_WEBHOOK_TOKEN and the Shopify app's SHOPIFY_CLIENT_SECRET;
  3. no token-shaped strings (Shopify/Google/GitHub tokens, private keys, bearer headers) and no hard-coded
     auth headers in HTTP/webhook nodes. One exception: WF1's "Fetch GraphQL page" may send exactly one header,
     X-Shopify-Access-Token, whose value is exactly the expression that reads the token sub-workflow's output;
  4. every run-now webhook uses Header Auth with the shared credential, and email addresses are placeholders only;
  5. the error workflow builds the execution link from N8N_EDITOR_BASE_URL, not a hard-coded host.

    python3 scripts/check_workflows_clean.py   -> exit 0 = clean, 1 = findings
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET_VARS = ["N8N_WEBHOOK_TOKEN", "STORE_API_TOKEN", "MOCK_ADMIN_TOKEN", "POSTGRES_PASSWORD", "GEMINI_API_KEY",
               "SHOPIFY_CLIENT_SECRET"]
PLACEHOLDERS = {"", "change-me", "shpat_change-me"}
TOKEN_PATTERNS = {
    # shpat_ (admin token), shpca_ (client-credentials token), shpss_ (client secret), shppa_ (private app)
    "Shopify-style token": r"shp(at|ss|ca|pa)_[0-9A-Za-z]{16,}",
    "Google API key": r"AIza[0-9A-Za-z_-]{35}",
    "GitHub token": r"gh[pousr]_[0-9A-Za-z]{36}",
    "private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "bearer token": r"Bearer\s+[A-Za-z0-9._~+/-]{12,}",
}
SECRET_KEYS = {"password", "token", "apikey", "api_key", "secret", "value", "accesstoken", "x-p8-token",
               "x-shopify-access-token", "x-goog-api-key", "authorization"}
# (workflow id, node name) -> the only header that node may send, and its only allowed (expression) value.
HEADER_EXCEPTIONS = {
    ("p8OrdersSync0001", "Fetch GraphQL page"):
        ("X-Shopify-Access-Token", "={{ $('Get Shopify token').first().json.token }}"),
}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
OK_EMAIL = re.compile(r"@([a-z0-9-]+\.)*(example\.(com|org|net)|example|test|invalid)$", re.I)


def env_secrets() -> dict[str, str]:
    values: dict[str, str] = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    values.update(os.environ)
    return {k: values[k] for k in SECRET_VARS if values.get(k, "") not in PLACEHOLDERS and len(values[k]) >= 6}


def walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, f"{path}[{i}]")
    else:
        yield path, obj


def check_file(path: Path, secrets: dict[str, str]) -> list[str]:
    text = path.read_text()
    wf = json.loads(text)
    problems = []
    for var, value in secrets.items():
        if value in text:
            problems.append(f"contains the value of {var}")
    for label, pat in TOKEN_PATTERNS.items():
        if re.search(pat, text):
            problems.append(f"contains a {label}")
    for m in EMAIL.finditer(text):
        if not OK_EMAIL.search(m.group(0)):
            problems.append(f"real-looking email address {m.group(0)}")
    for node in wf.get("nodes", []):
        name = node.get("name")
        for ctype, ref in (node.get("credentials") or {}).items():
            if set(ref) - {"id", "name"}:
                problems.append(f"node {name!r}: credential {ctype} carries more than id/name: {sorted(ref)}")
        params = node.get("parameters", {})
        t = node.get("type", "")
        if t == "n8n-nodes-base.webhook":
            if params.get("authentication") != "headerAuth" or "httpHeaderAuth" not in (node.get("credentials") or {}):
                problems.append(f"node {name!r}: run-now webhook without Header Auth credential")
        if t == "n8n-nodes-base.httpRequest":
            allowed = HEADER_EXCEPTIONS.get((wf.get("id"), name))
            if params.get("sendQuery"):
                problems.append(f"node {name!r}: sends custom query parameters; auth must come from a credential")
            if params.get("sendHeaders"):
                if not allowed:
                    problems.append(f"node {name!r}: sends custom headers; auth must come from a credential")
                else:
                    headers = (params.get("headerParameters") or {}).get("parameters") or []
                    if params.get("specifyHeaders", "keypair") != "keypair" or [
                            (h.get("name"), h.get("value")) for h in headers] != [allowed]:
                        problems.append(f"node {name!r}: may only send header {allowed[0]} = {allowed[1]}")
        for p, v in walk(params):
            key = p.rsplit(".", 1)[-1].lower()
            if key in SECRET_KEYS - {"value"} and isinstance(v, str) and v and not v.startswith("="):
                problems.append(f"node {name!r}: literal secret-like parameter {p}")
    if wf.get("id") == "p8ErrorHandler01":
        if "N8N_EDITOR_BASE_URL" not in text:
            problems.append("error workflow does not use N8N_EDITOR_BASE_URL for the execution link")
        if re.search(r"https?://(localhost|127\.0\.0\.1):\d+/workflow/", text):
            problems.append("error workflow hard-codes the editor host in the execution link")
    return problems


def main() -> int:
    secrets = env_secrets()
    files = sorted((ROOT / "workflows").glob("*.json"))
    bad = 0
    for f in files:
        for p in check_file(f, secrets):
            print(f"WORKFLOW-CHECK: {f.relative_to(ROOT)}: {p}")
            bad += 1
    if not files:
        print("WORKFLOW-CHECK: no workflows found")
        return 1
    if bad:
        return 1
    print(f"WORKFLOW-CHECK: clean ({len(files)} workflows, {len(secrets)} secret values from .env checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
