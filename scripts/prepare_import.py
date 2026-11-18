"""Build what scripts/import_workflows.sh loads into n8n, from the environment (.env values).

    prepare_import.py creds OUT          # n8n credential file (mode 0600); the only place secrets are written
    prepare_import.py workflow IN OUT    # a workflow JSON adjusted for STORE_SOURCE

STORE_SOURCE=shopify changes two things at import time (the committed JSON stays source-neutral):
  * the Postgres credential points at SHOPIFY_POSTGRES_DB (default p8_shopify), so real-store rows never mix
    with the mock store's;
  * WF1 (orders sync) saves failed executions only: a successful run's data would keep the Shopify access token
    in n8n's execution history. (The token sub-workflow saves nothing at all, in either mode.)
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping

ORDERS_SYNC_ID = "p8OrdersSync0001"


def store_source(env: Mapping[str, str]) -> str:
    src = (env.get("STORE_SOURCE") or "mock").strip().lower()
    if src not in {"mock", "shopify"}:
        raise SystemExit(f"STORE_SOURCE must be mock or shopify, got {src!r}")
    return src


def database(env: Mapping[str, str]) -> str:
    if store_source(env) == "shopify":
        return env.get("SHOPIFY_POSTGRES_DB") or "p8_shopify"
    return env["POSTGRES_DB"]


def credentials(env: Mapping[str, str]) -> list[dict]:
    e = env
    shopify_body = dict(client_id=e.get("SHOPIFY_CLIENT_ID") or "not-configured",
                        client_secret=e.get("SHOPIFY_CLIENT_SECRET") or "not-configured")
    return [
        {"id": "p8PostgresCred01", "name": "P8 Postgres", "type": "postgres",
         "data": {"host": e.get("PG_HOST", "postgres"), "port": int(e.get("PG_PORT_IN", "5432")),
                  "database": database(e), "user": e["POSTGRES_USER"], "password": e["POSTGRES_PASSWORD"],
                  "ssl": "disable"}},
        {"id": "p8StoreApiCred01", "name": "P8 store API", "type": "httpHeaderAuth",
         "data": {"name": "X-Shopify-Access-Token", "value": e["STORE_API_TOKEN"]}},
        {"id": "p8WebhookCred001", "name": "P8 run-now token", "type": "httpHeaderAuth",
         "data": {"name": "X-P8-Token", "value": e["N8N_WEBHOOK_TOKEN"]}},
        {"id": "p8MailpitSmtp001", "name": "P8 SMTP (Mailpit)", "type": "smtp",
         "data": {"host": e.get("SMTP_HOST", "mailpit"), "port": int(e.get("SMTP_PORT_IN", "1025")),
                  "secure": False, "disableStartTls": True, "user": "", "password": ""}},
        {"id": "p8GeminiCred0001", "name": "P8 Gemini API", "type": "httpHeaderAuth",
         "data": {"name": "x-goog-api-key", "value": e.get("GEMINI_API_KEY") or "not-configured"}},
        # Shopify Dev Dashboard app (client credentials grant). The client secret lives only in this credential.
        # n8n's Custom Auth adds these fields to the token request's JSON body (Shopify documents a form-encoded
        # body and accepts JSON; see README "Real Shopify store").
        {"id": "p8ShopifyApp0001", "name": "P8 Shopify app", "type": "httpCustomAuth",
         "data": {"json": json.dumps({"body": shopify_body})}},
    ]


def prepare_workflow(wf: dict, env: Mapping[str, str]) -> dict:
    wf = json.loads(json.dumps(wf))
    if wf.get("id") == ORDERS_SYNC_ID and store_source(env) == "shopify":
        wf["settings"].update({"saveDataSuccessExecution": "none", "saveDataErrorExecution": "all",
                               "saveManualExecutions": False})
    return wf


def write_private(path: str, data) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)


def main(argv: list[str]) -> int:
    if argv[:1] == ["creds"] and len(argv) == 2:
        write_private(argv[1], credentials(os.environ))
    elif argv[:1] == ["workflow"] and len(argv) == 3:
        with open(argv[1]) as f:
            wf = json.load(f)
        with open(argv[2], "w") as f:
            json.dump(prepare_workflow(wf, os.environ), f)
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
