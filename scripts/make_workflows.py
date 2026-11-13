"""Generates the six importable n8n workflows into n8n/workflows/. Re-run after editing:  python scripts/make_workflows.py
n8n here is only the scheduler + error handler. The business rules live in the API (so they are unit-tested)."""
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "n8n" / "workflows"
API = "http://api:8000"  # Docker network name. If n8n runs outside Docker, change to http://localhost:8000
KEY_EXPR = "={{ $env.CHEAPEDEAL_INTERNAL_KEY }}"


def note(text, x, y, w=420, h=180):
    return {"parameters": {"content": text, "height": h, "width": w}, "id": f"note-{x}", "name": "Read me",
            "type": "n8n-nodes-base.stickyNote", "typeVersion": 1, "position": [x, y]}


def schedule(interval, label):
    return {"parameters": {"rule": {"interval": [interval]}}, "id": "trigger", "name": label,
            "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2, "position": [0, 0]}


def call(path, name="Call Cheape Deal", timeout_ms=300000):
    return {"parameters": {"method": "POST", "url": f"{API}{path}", "sendHeaders": True,
                           "headerParameters": {"parameters": [{"name": "X-Internal-Key", "value": KEY_EXPR}]},
                           "options": {"timeout": timeout_ms}},
            "id": "call", "name": name, "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [300, 0],
            "retryOnFail": True, "maxTries": 3, "waitBetweenTries": 5000}


def workflow(name, nodes, links):
    conns = {}
    for a, b in links:
        conns.setdefault(a, {"main": [[]]})["main"][0].append({"node": b, "type": "main", "index": 0})
    return {"name": name, "nodes": nodes, "connections": conns, "active": False,
            "settings": {"executionOrder": "v1", "saveManualExecutions": True}, "pinData": {}, "tags": []}


JOBS = [
    ("01_order_intake_sweep.json", "Cheape Deal 01 - Missed-order sweep", {"field": "minutes", "minutesInterval": 5},
     "Every 5 min", "/internal/jobs/intake-sweep",
     "Safety net for the store webhook. Imports any paid store order from the last 48h that never arrived by webhook (and alerts you, so you can fix the webhook)."),
    ("02_order_to_supplier.json", "Cheape Deal 02 - Orders to supplier", {"field": "minutes", "minutesInterval": 2},
     "Every 2 min", "/internal/jobs/process-pending",
     "Validates and sends new orders to the supplier, and re-tries failed ones when their back-off time is due. Respects the kill switch and DRY-RUN."),
    ("03_tracking_sync.json", "Cheape Deal 03 - Tracking sync", {"field": "hours", "hoursInterval": 2},
     "Every 2 hours", "/internal/jobs/tracking-sync",
     "Reads supplier order status, updates the store, and tells the customer once per shipped/delivered change (via WooCommerce customer notes)."),
    ("04_stock_price_sync.json", "Cheape Deal 04 - Stock and price sync", {"field": "hours", "hoursInterval": 8},
     "Every 8 hours", "/internal/jobs/stock-price-sync",
     "Hides out-of-stock products, reprices when supplier cost changes, and hides anything whose margin would fall under the minimum."),
    ("06_nightly_reconcile.json", "Cheape Deal 06 - Nightly reconciliation", {"field": "cron", "expression": "0 2 * * *"},
     "Every night 02:00", "/internal/jobs/reconcile",
     "Compares paid store orders with Cheape Deal and the supplier. Anything missing, stuck or inconsistent becomes an alert."),
]

OUT.mkdir(parents=True, exist_ok=True)
for fname, title, interval, label, path, desc in JOBS:
    nodes = [schedule(interval, label), call(path),
             note(f"**{title}**\n\n{desc}\n\nSet this workflow's *Error Workflow* (Settings) to 'Cheape Deal 05 - Error handler', then switch it to Active.", -20, -260)]
    (OUT / fname).write_text(json.dumps(workflow(title, nodes, [(label, "Call Cheape Deal")]), indent=2), encoding="utf-8")

err_body = ("={{ ({ workflow: $json.workflow.name, level: 'error', "
            "message: String($json.execution.error.message) + ' (last node: ' + $json.execution.lastNodeExecuted + ')' }) }}")
err_nodes = [
    {"parameters": {}, "id": "trigger", "name": "On workflow error", "type": "n8n-nodes-base.errorTrigger", "typeVersion": 1, "position": [0, 0]},
    {"parameters": {"method": "POST", "url": f"{API}/internal/alerts", "sendHeaders": True,
                    "headerParameters": {"parameters": [{"name": "X-Internal-Key", "value": KEY_EXPR}]},
                    "sendBody": True, "specifyBody": "json", "jsonBody": err_body, "options": {"timeout": 30000}},
     "id": "call", "name": "Send alert to Cheape Deal", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [300, 0],
     "retryOnFail": True, "maxTries": 3, "waitBetweenTries": 5000},
    note("**Cheape Deal 05 - Error handler**\n\nOther workflows point to this one (Settings > Error Workflow). When any of them fails, the error lands in Alerts and on Telegram. Leave this workflow *Active*.", -20, -260),
]
(OUT / "05_error_handler.json").write_text(json.dumps(
    workflow("Cheape Deal 05 - Error handler", err_nodes, [("On workflow error", "Send alert to Cheape Deal")]), indent=2), encoding="utf-8")
print("wrote", sorted(p.name for p in OUT.glob("*.json")))
