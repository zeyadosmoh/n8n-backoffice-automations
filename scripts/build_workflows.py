"""Build the n8n workflow JSON files in workflows/ from this definition and the Code-node sources in workflows/src/.

    python3 scripts/build_workflows.py          # write workflows/*.json
    python3 scripts/build_workflows.py --check  # exit 1 if a committed JSON file is out of date

The JSON files are the importable exports (scripts/import_workflows.sh). They reference credentials by id and name
only; no token, password or key is ever written into them.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "workflows"
SRC = OUT / "src"
NS = uuid.UUID("6f1c3a52-8b8e-4d55-9a57-2c1f3e0b8d11")

CRED = {
    "postgres": {"postgres": {"id": "p8PostgresCred01", "name": "P8 Postgres"}},
    "store": {"httpHeaderAuth": {"id": "p8StoreApiCred01", "name": "P8 store API"}},
    "webhook": {"httpHeaderAuth": {"id": "p8WebhookCred001", "name": "P8 run-now token"}},
    "smtp": {"smtp": {"id": "p8MailpitSmtp001", "name": "P8 SMTP (Mailpit)"}},
    "gemini": {"httpHeaderAuth": {"id": "p8GeminiCred0001", "name": "P8 Gemini API"}},
}
ERROR_WORKFLOW_ID = "p8ErrorHandler01"
FOOTER = "P8 back-office automations · synthetic data"


def js(name: str) -> str:
    return (SRC / f"{name}.js").read_text()


class Workflow:
    def __init__(self, wf_id: str, name: str, tags: list[str] | None = None, error_workflow: bool = True):
        self.id, self.name, self.nodes, self.connections = wf_id, name, [], {}
        self.settings = {"executionOrder": "v1", "timezone": "Africa/Cairo", "saveManualExecutions": True,
                         "callerPolicy": "workflowsFromSameOwner"}
        if error_workflow:
            self.settings["errorWorkflow"] = ERROR_WORKFLOW_ID

    def node(self, name, type_, version, params, pos, creds=None, **extra):
        n = {"parameters": params, "id": str(uuid.uuid5(NS, f"{self.id}/{name}")), "name": name,
             "type": type_, "typeVersion": version, "position": list(pos)}
        if creds:
            n["credentials"] = CRED[creds]
        n.update(extra)
        self.nodes.append(n)
        return name

    def connect(self, a, b, out=0):
        main = self.connections.setdefault(a, {}).setdefault("main", [])
        while len(main) <= out:
            main.append([])
        main[out].append({"node": b, "type": "main", "index": 0})

    def chain(self, *names):
        for a, b in zip(names, names[1:]):
            self.connect(a, b)

    def to_json(self):
        return {"id": self.id, "name": self.name, "active": False, "nodes": self.nodes,
                "connections": self.connections, "settings": self.settings, "pinData": {},
                "meta": {"templateCredsSetupCompleted": True}}

    # common nodes -------------------------------------------------------------------------------
    def webhook(self, name, path, pos):
        return self.node(name, "n8n-nodes-base.webhook", 2,
                         {"httpMethod": "POST", "path": path, "authentication": "headerAuth",
                          "responseMode": "lastNode", "options": {}},
                         pos, creds="webhook", webhookId=str(uuid.uuid5(NS, f"webhook/{path}")))

    def code(self, name, src, pos, each=False, **extra):
        p = {"jsCode": js(src)}
        if each:
            p["mode"] = "runOnceForEachItem"
        return self.node(name, "n8n-nodes-base.code", 2, p, pos, **extra)

    def sql(self, name, query, pos, params=None, **extra):
        opts = {"queryReplacement": params} if params else {}
        return self.node(name, "n8n-nodes-base.postgres", 2.6,
                         {"operation": "executeQuery", "query": query, "options": opts}, pos, creds="postgres",
                         **extra)

    def if_true(self, name, left, right, pos, op="equals", kind="string"):
        return self.node(name, "n8n-nodes-base.if", 2.2, {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
                "conditions": [{"id": str(uuid.uuid5(NS, f"{self.id}/{name}/c")), "leftValue": left,
                                "rightValue": right, "operator": {"type": kind, "operation": op}}],
                "combinator": "and"},
            "options": {}}, pos)

    def email(self, name, subject, html, pos, attachments=None, text=None):
        opts = {"appendAttribution": False}
        if attachments:
            opts["attachments"] = attachments
        p = {"fromEmail": "={{ $env.ALERT_EMAIL_FROM }}", "toEmail": "={{ $env.ALERT_EMAIL_TO }}",
             "subject": subject, "emailFormat": "both" if text else "html", "html": html, "options": opts}
        if text:
            p["text"] = text
        return self.node(name, "n8n-nodes-base.emailSend", 2.1, p, pos, creds="smtp")


# ---------------------------------------------------------------------------------------------------------------
def orders_sync() -> Workflow:
    w = Workflow("p8OrdersSync0001", "P8 · Orders sync (hourly) + daily Excel export")
    # sync loop
    w.node("Every hour", "n8n-nodes-base.scheduleTrigger", 1.2,
           {"rule": {"interval": [{"field": "cronExpression", "expression": "5 * * * *"}]}}, (0, 0))
    w.webhook("Run now (webhook)", "p8/orders-sync", (0, 200))
    w.sql("Load cursors",
          "select p8_sync_since('orders') as orders_since, p8_sync_since('inventory_levels') as inventory_since;",
          (240, 100))
    w.code("Start streams", "orders_start_streams", (480, 100))
    w.node("Next request", "n8n-nodes-base.noOp", 1, {}, (720, 100))
    w.node("Fetch page", "n8n-nodes-base.httpRequest", 4.2, {
        "url": "={{ $json.url }}",
        "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth",
        "options": {"timeout": 30000,
                    "response": {"response": {"fullResponse": True, "neverError": True, "responseFormat": "json"}}},
    }, (960, 100), creds="store", onError="continueRegularOutput")
    w.code("Check response", "orders_check_response", (1200, 100), each=True)
    w.if_true("Retry or apply?", "={{ $json.action }}", "retry", (1440, 100))
    w.node("Back off", "n8n-nodes-base.wait", 1.1,
           {"amount": "={{ $json.waitSeconds }}", "unit": "seconds"}, (1680, -60),
           webhookId=str(uuid.uuid5(NS, "wait/orders")))
    w.sql("Apply page (one transaction)",
          "select case $1 when 'orders' then p8_sync_orders_page($2::jsonb)\n"
          "            else p8_sync_inventory_page($2::jsonb) end as result;",
          (1680, 200), params="={{ [ $json.stream, JSON.stringify($json.payload) ] }}")
    w.code("Next page?", "orders_next_page", (1920, 200), each=True)
    w.if_true("More to fetch?", "={{ $json.done }}", False, (2160, 200), op="false", kind="boolean")
    w.code("Sync summary", "orders_summary", (2400, 300))
    w.connect("Every hour", "Load cursors")
    w.connect("Run now (webhook)", "Load cursors")
    w.chain("Load cursors", "Start streams", "Next request", "Fetch page", "Check response", "Retry or apply?")
    w.connect("Retry or apply?", "Back off", 0)
    w.connect("Retry or apply?", "Apply page (one transaction)", 1)
    w.connect("Back off", "Next request")
    w.chain("Apply page (one transaction)", "Next page?", "More to fetch?")
    w.connect("More to fetch?", "Next request", 0)
    w.connect("More to fetch?", "Sync summary", 1)

    # daily Excel export
    w.node("Daily 23:55", "n8n-nodes-base.scheduleTrigger", 1.2,
           {"rule": {"interval": [{"field": "cronExpression", "expression": "55 23 * * *"}]}}, (0, 600))
    w.webhook("Export now (webhook)", "p8/orders-export", (0, 800))
    w.code("Export day", "orders_export_day", (240, 700))
    w.sql("Load order lines", "select * from p8_orders_export($1::date);", (480, 700),
          params="={{ [ $json.day ] }}", alwaysOutputData=True)
    w.node("To Excel", "n8n-nodes-base.convertToFile", 1.1, {
        "operation": "xlsx",
        "options": {"fileName": "=orders_{{ $('Export day').first().json.day }}.xlsx",
                    "sheetName": "Orders (synthetic data)", "headerRow": True},
    }, (720, 700))
    w.node("Save to exports/", "n8n-nodes-base.readWriteFile", 1, {
        "operation": "write", "fileName": "={{ $('Export day').first().json.file }}", "dataPropertyName": "data",
        "options": {},
    }, (960, 700))
    w.code("Export summary", "orders_export_summary", (1200, 700))
    w.connect("Daily 23:55", "Export day")
    w.connect("Export now (webhook)", "Export day")
    w.chain("Export day", "Load order lines", "To Excel", "Save to exports/", "Export summary")
    return w


def low_stock_alert() -> Workflow:
    w = Workflow("p8LowStock000001", "P8 · Low-stock alert (daily 08:00 Africa/Cairo)")
    w.node("Daily 08:00 Cairo", "n8n-nodes-base.scheduleTrigger", 1.2,
           {"rule": {"interval": [{"field": "cronExpression", "expression": "0 8 * * *"}]}}, (0, 0))
    w.webhook("Run now (webhook)", "p8/low-stock", (0, 200))
    w.sql("Claim today's alerts", "select * from p8_claim_low_stock_alerts();", (240, 100), alwaysOutputData=True)
    w.code("Build email", "low_stock_email", (480, 100))
    w.if_true("Anything to send?", "={{ $json.send }}", True, (720, 100), op="true", kind="boolean")
    w.email("Send low-stock email", "={{ $json.subject }}", "={{ $json.html }}", (960, 0), text="={{ $json.text }}")
    w.sql("Mark alerts sent", "select p8_mark_alerts_sent($1::bigint[]) as marked;", (1200, 0),
          params="={{ [ '{' + $('Build email').first().json.ids.join(',') + '}' ] }}")
    w.code("Alert summary", "low_stock_summary", (1440, 0))
    w.code("Nothing to send", "low_stock_nothing", (960, 220))
    w.connect("Daily 08:00 Cairo", "Claim today's alerts")
    w.connect("Run now (webhook)", "Claim today's alerts")
    w.chain("Claim today's alerts", "Build email", "Anything to send?")
    w.connect("Anything to send?", "Send low-stock email", 0)
    w.connect("Anything to send?", "Nothing to send", 1)
    w.chain("Send low-stock email", "Mark alerts sent", "Alert summary")
    return w


def switch_kind(w: Workflow, name, pos, cases):
    rules = []
    for value, label in cases:
        rules.append({
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
                "conditions": [{"id": str(uuid.uuid5(NS, f"{w.id}/{name}/{value}")), "leftValue": "={{ $json.kind }}",
                                "rightValue": value, "operator": {"type": "string", "operation": "equals"}}],
                "combinator": "and"},
            "renameOutput": True, "outputKey": label})
    return w.node(name, "n8n-nodes-base.switch", 3.2,
                  {"rules": {"values": rules},
                   "options": {"fallbackOutput": "extra", "renameFallbackOutput": "Other / no file"}}, pos)


def price_list() -> Workflow:
    w = Workflow("p8PriceList00001", "P8 · Supplier price list (form upload -> validate -> apply)")
    w.node("Price list upload form", "n8n-nodes-base.formTrigger", 2.3, {
        "formTitle": "Supplier price list upload",
        "formDescription": "Upload the supplier price list as CSV or XLSX. Needed columns: a price column and a SKU "
                           "or product-name column. Every valid row is applied in one transaction; you get a summary "
                           "email with a rejects file (one reason per row). Demo store with synthetic data.",
        "formFields": {"values": [{"fieldLabel": "Price list file", "fieldType": "file", "multipleFiles": False,
                                   "acceptFileTypes": ".csv, .xlsx", "requiredField": True}]},
        "responseMode": "lastNode",
        "options": {"path": "p8-price-list", "appendAttribution": False, "buttonLabel": "Upload and apply"},
    }, (0, 200), webhookId=str(uuid.uuid5(NS, "form/p8-price-list")))
    w.code("Detect file type", "price_detect_file", (240, 200))
    switch_kind(w, "CSV or XLSX?", (480, 200), [("csv", "CSV"), ("xlsx", "XLSX")])
    w.node("Read CSV", "n8n-nodes-base.extractFromFile", 1.1,
           {"operation": "csv", "binaryPropertyName": "file", "options": {"headerRow": True}}, (720, 40),
           alwaysOutputData=True)
    w.node("Read XLSX", "n8n-nodes-base.extractFromFile", 1.1,
           {"operation": "xlsx", "binaryPropertyName": "file", "options": {"headerRow": True}}, (720, 200),
           alwaysOutputData=True)
    w.code("Collect rows", "price_collect_rows", (960, 200))
    w.sql("Validate + diff", "select p8_validate_price_list($1, $2::jsonb, $3::boolean, $4) as report;", (1200, 200),
          params="={{ [ $json.fileName, $json.rows === null ? null : JSON.stringify($json.rows), $json.ai, "
                 "$json.fileError ] }}")
    w.if_true("Rows for AI?", "={{ $json.report.rows_pending_ai }}", 0, (1440, 200), op="gt", kind="number")
    w.sql("Load catalog", "select sku, name from products order by sku;", (1680, 20))
    w.code("Build AI prompt", "price_ai_prompt", (1920, 20))
    w.node("Ask Gemini", "n8n-nodes-base.httpRequest", 4.2, {
        "method": "POST", "url": "={{ $json.url }}",
        "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth",
        "sendBody": True, "specifyBody": "json", "jsonBody": "={{ JSON.stringify($json.body) }}",
        "options": {"timeout": 60000,
                    "response": {"response": {"fullResponse": True, "neverError": True, "responseFormat": "json"}}},
    }, (2160, 20), creds="gemini", onError="continueRegularOutput")
    w.code("Parse AI answer", "price_ai_parse", (2400, 20))
    w.sql("Record AI matches (review only)",
          "select p8_record_ai_matches($1, $2::jsonb, $3::numeric, $4) as report;", (2640, 20),
          params="={{ [ $json.importId, JSON.stringify($json.matches), $json.threshold, $json.error ] }}")
    w.sql("Apply valid rows (one transaction)", "select p8_apply_price_list($1) as report;", (2880, 200),
          params="={{ [ $json.report.import_id ] }}")
    w.code("Build summary", "price_summary", (3120, 200))
    w.email("Send summary email", "={{ $json.subject }}", "={{ $json.html }}", (3360, 200),
            attachments="={{ $json.attachments }}")
    w.node("Show result to uploader", "n8n-nodes-base.form", 2.3, {
        "operation": "completion", "respondWith": "text", "completionTitle": "Price list processed",
        "completionMessage": "={{ (r => r.file_error ? 'File rejected: ' + r.file_error + '. Nothing was changed.' : "
                             "r.rows_changed + ' price(s) updated, ' + r.rows_unchanged + ' unchanged, ' + "
                             "r.rows_rejected + ' rejected, ' + r.rows_review + ' AI suggestion(s) to review. "
                             "A summary email with the details was sent.')($('Build summary').first().json.report) }}",
        "options": {}}, (3600, 200), webhookId=str(uuid.uuid5(NS, "form/p8-price-list/done")))
    w.chain("Price list upload form", "Detect file type", "CSV or XLSX?")
    w.connect("CSV or XLSX?", "Read CSV", 0)
    w.connect("CSV or XLSX?", "Read XLSX", 1)
    w.connect("CSV or XLSX?", "Collect rows", 2)
    w.connect("Read CSV", "Collect rows")
    w.connect("Read XLSX", "Collect rows")
    w.chain("Collect rows", "Validate + diff", "Rows for AI?")
    w.connect("Rows for AI?", "Load catalog", 0)
    w.connect("Rows for AI?", "Apply valid rows (one transaction)", 1)
    w.chain("Load catalog", "Build AI prompt", "Ask Gemini", "Parse AI answer", "Record AI matches (review only)",
            "Apply valid rows (one transaction)")
    w.chain("Apply valid rows (one transaction)", "Build summary", "Send summary email", "Show result to uploader")
    return w


def error_handler() -> Workflow:
    w = Workflow(ERROR_WORKFLOW_ID, "P8 · Error handler (failure email)", error_workflow=False)
    w.node("On workflow error", "n8n-nodes-base.errorTrigger", 1, {}, (0, 0))
    w.code("Format alert", "error_format", (240, 0))
    w.email("Send failure email", "={{ $json.subject }}", "={{ $json.html }}", (480, 0), text="={{ $json.text }}")
    w.chain("On workflow error", "Format alert", "Send failure email")
    return w


WORKFLOWS = {
    "orders_sync": orders_sync,
    "low_stock_alert": low_stock_alert,
    "supplier_price_list": price_list,
    "error_handler": error_handler,
}


def render() -> dict[str, str]:
    return {f"{k}.json": json.dumps(f().to_json(), indent=2, ensure_ascii=False) + "\n" for k, f in WORKFLOWS.items()}


def main(argv: list[str]) -> int:
    files = render()
    if "--check" in argv:
        stale = [n for n, body in files.items() if not (OUT / n).exists() or (OUT / n).read_text() != body]
        if stale:
            print("out of date (run scripts/build_workflows.py):", ", ".join(stale))
            return 1
        print(f"workflows up to date ({len(files)} files)")
        return 0
    for n, body in files.items():
        (OUT / n).write_text(body)
        print("wrote", OUT.relative_to(ROOT) / n)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
