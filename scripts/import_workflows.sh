#!/usr/bin/env bash
# Load the P8 credentials and workflows into a fresh n8n container, publish the workflows and restart n8n.
#
#   scripts/import_workflows.sh                      # compose project "p8", values from .env
#   ENV_FILE=.e2e/.env PROJECT=p8-e2e scripts/import_workflows.sh
#   N8N_CONTAINER=<id> PG_HOST=postgres SMTP_HOST=mailpit scripts/import_workflows.sh   # e.g. CI service containers
#   WAIT_ONLY=1 scripts/import_workflows.sh          # only wait until every webhook/form is registered
#
# The credential file is built from .env in a private temp file, copied into the container, imported and deleted
# on both sides. The workflow JSON files contain credential ids and names only, never secrets.
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE=${ENV_FILE:-.env}
PROJECT=${PROJECT:-p8}
[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE (run: python3 scripts/setup_env.py)" >&2; exit 1; }
set -a; . "$ENV_FILE"; set +a

C=${N8N_CONTAINER:-$(docker compose -p "$PROJECT" ps -q n8n)}
[ -n "$C" ] || { echo "n8n container for project $PROJECT is not running" >&2; exit 1; }
RESTART=${RESTART:-1}

# Webhook paths of the published workflows; n8n is "ready" only when none of them answers 404 any more.
wait_for_webhooks() {
  local paths
  paths=$(python3 -c '
import json, glob
for f in sorted(glob.glob("workflows/*.json")):
    for n in json.load(open(f))["nodes"]:
        if n["type"] == "n8n-nodes-base.webhook":
            print("POST webhook/" + n["parameters"]["path"])
        elif n["type"] == "n8n-nodes-base.formTrigger":
            print("GET form/" + n["parameters"]["options"]["path"])
')
  for _ in $(seq 1 120); do
    if docker exec -e PATHS="$paths" "$C" node -e '
      const paths = process.env.PATHS.split("\n").filter(Boolean).map(l => l.split(" "));
      Promise.all(paths.map(([m, p]) => fetch("http://127.0.0.1:5678/" + p, {method: m}).then(r => r.status)))
        .then(s => process.exit(s.every(x => x !== 404) ? 0 : 1)).catch(() => process.exit(1));
    ' >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "n8n did not register the webhooks in time" >&2
  return 1
}

# The server must finish its own start-up (first start = database migrations) before any CLI command touches the
# same database, or the two race on the migrations.
wait_for_ready() {
  for _ in $(seq 1 180); do
    if docker exec "$C" node -e '
      fetch("http://127.0.0.1:5678/healthz/readiness").then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1));
    ' >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "n8n did not become ready" >&2
  return 1
}

if [ "${WAIT_ONLY:-0}" = "1" ]; then
  wait_for_webhooks && echo "n8n webhooks ready"
  exit $?
fi


wait_for_ready

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"; docker exec -u root "$C" rm -rf /tmp/p8-import >/dev/null 2>&1 || true' EXIT
chmod 700 "$TMP"

PG_HOST=${PG_HOST:-postgres} PG_PORT_IN=${PG_PORT_IN:-5432} SMTP_HOST=${SMTP_HOST:-mailpit} \
SMTP_PORT_IN=${SMTP_PORT_IN:-1025} python3 - "$TMP/creds.cred.json" <<'PY'
import json, os, sys
e = os.environ
creds = [
    {"id": "p8PostgresCred01", "name": "P8 Postgres", "type": "postgres",
     "data": {"host": e["PG_HOST"], "port": int(e["PG_PORT_IN"]), "database": e["POSTGRES_DB"],
              "user": e["POSTGRES_USER"], "password": e["POSTGRES_PASSWORD"], "ssl": "disable"}},
    {"id": "p8StoreApiCred01", "name": "P8 store API", "type": "httpHeaderAuth",
     "data": {"name": "X-Shopify-Access-Token", "value": e["STORE_API_TOKEN"]}},
    {"id": "p8WebhookCred001", "name": "P8 run-now token", "type": "httpHeaderAuth",
     "data": {"name": "X-P8-Token", "value": e["N8N_WEBHOOK_TOKEN"]}},
    {"id": "p8MailpitSmtp001", "name": "P8 SMTP (Mailpit)", "type": "smtp",
     "data": {"host": e["SMTP_HOST"], "port": int(e["SMTP_PORT_IN"]), "secure": False, "disableStartTls": True,
              "user": "", "password": ""}},
    {"id": "p8GeminiCred0001", "name": "P8 Gemini API", "type": "httpHeaderAuth",
     "data": {"name": "x-goog-api-key", "value": e.get("GEMINI_API_KEY") or "not-configured"}},
]
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(creds, f)
PY

# Files are streamed in (not `docker cp`) so the container's own user creates them: `docker cp` keeps the host
# owner (e.g. uid 1001 on GitHub runners), and n8n (uid 1000) could not read a 0600 credential file.
put() { docker exec -i "$C" sh -c "umask 077; cat > '$2'" < "$1"; }
docker exec "$C" mkdir -p /tmp/p8-import
put "$TMP/creds.cred.json" /tmp/p8-import/creds.json
rm -f "$TMP/creds.cred.json"
out=$(docker exec "$C" n8n import:credentials --input=/tmp/p8-import/creds.json 2>&1) || { echo "$out" >&2; exit 1; }
echo "$out" | grep -E "Successfully" || true
docker exec -u root "$C" rm -f /tmp/p8-import/creds.json

ids=()
for wf in workflows/*.json; do
  put "$wf" "/tmp/p8-import/$(basename "$wf")"
  out=$(docker exec "$C" n8n import:workflow --input="/tmp/p8-import/$(basename "$wf")" 2>&1) \
    || { echo "$out" >&2; echo "import failed: $wf" >&2; exit 1; }
  echo "$out" | grep -E "Successfully" || true
  ids+=("$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$wf")")
done
for id in "${ids[@]}"; do
  out=$(docker exec "$C" n8n publish:workflow --id="$id" 2>&1) || { echo "$out" >&2; echo "publish failed: $id" >&2; exit 1; }
  echo "$out" | grep -iE "publishing" | tail -1
done

if [ "$RESTART" = "1" ]; then
  docker restart "$C" >/dev/null
  echo "restarted n8n; waiting for its webhooks"
  wait_for_webhooks
  echo "n8n ready: ${#ids[@]} workflows imported and published"
fi
