import azure.functions as func
import requests
import json
import base64
import os
import logging
import hmac
import hashlib

app = func.FunctionApp()

# ─────────────────────────────────────────────────────────────
# HELPER: Map Jira Priority → ADO Priority
# ─────────────────────────────────────────────────────────────
def map_priority(jira_priority: str) -> int:
    mapping = {
        "Highest": 1,
        "High"   : 1,
        "Medium" : 2,
        "Low"    : 3,
        "Lowest" : 4
    }
    return mapping.get(jira_priority, 2)


# ─────────────────────────────────────────────────────────────
# HELPER: Build ADO Auth Header using PAT
# ─────────────────────────────────────────────────────────────
def get_ado_headers() -> dict:
    pat   = os.environ["ADO_PAT"]
    token = base64.b64encode(f":{pat}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type" : "application/json-patch+json"
    }


# ─────────────────────────────────────────────────────────────
# HELPER: Find existing ADO Work Item by Jira Key (via Tags)
# ─────────────────────────────────────────────────────────────
def find_work_item(jira_key: str):
    org     = os.environ["ADO_ORG"]
    project = os.environ["ADO_PROJECT"]

    wiql_url = (
        f"https://dev.azure.com/{org}/{project}"
        f"/_apis/wit/wiql?api-version=7.1"
    )

    query = {
        "query": (
            f"SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.Tags] CONTAINS '{jira_key}'"
        )
    }

    resp  = requests.post(wiql_url, headers=get_ado_headers(), json=query)
    items = resp.json().get("workItems", []) if resp.status_code == 200 else []

    return items[0]["id"] if items else None


# ─────────────────────────────────────────────────────────────
# HELPER: Create new ADO Work Item
# ─────────────────────────────────────────────────────────────
def create_work_item(summary: str, description: str, priority: int, jira_key: str):
    org     = os.environ["ADO_ORG"]
    project = os.environ["ADO_PROJECT"]

    url = (
        f"https://dev.azure.com/{org}/{project}"
        f"/_apis/wit/workitems/$Task?api-version=7.1"
    )

    body = [
        {
            "op"   : "add",
            "path" : "/fields/System.Title",
            "value": f"[{jira_key}] {summary}"
        },
        {
            "op"   : "add",
            "path" : "/fields/System.Description",
            "value": description
        },
        {
            "op"   : "add",
            "path" : "/fields/Microsoft.VSTS.Common.Priority",
            "value": priority
        },
        {
            "op"   : "add",
            "path" : "/fields/System.Tags",
            "value": jira_key
        }
    ]

    resp = requests.post(url, headers=get_ado_headers(), json=body)
    logging.info(f"CREATE → ADO HTTP {resp.status_code} | Jira Key: {jira_key}")
    return resp


# ─────────────────────────────────────────────────────────────
# HELPER: Update existing ADO Work Item
# ─────────────────────────────────────────────────────────────
def update_work_item(work_item_id: int, summary: str, description: str, priority: int, jira_key: str):
    org = os.environ["ADO_ORG"]

    url = (
        f"https://dev.azure.com/{org}"
        f"/_apis/wit/workitems/{work_item_id}?api-version=7.1"
    )

    body = [
        {
            "op"   : "replace",
            "path" : "/fields/System.Title",
            "value": f"[{jira_key}] {summary}"
        },
        {
            "op"   : "replace",
            "path" : "/fields/System.Description",
            "value": description
        },
        {
            "op"   : "replace",
            "path" : "/fields/Microsoft.VSTS.Common.Priority",
            "value": priority
        }
    ]

    resp = requests.patch(url, headers=get_ado_headers(), json=body)
    logging.info(f"UPDATE → ADO HTTP {resp.status_code} | Work Item ID: {work_item_id}")
    return resp


# ─────────────────────────────────────────────────────────────
# MAIN: HTTP Trigger — receives Jira webhook POST
# ─────────────────────────────────────────────────────────────
@app.route(route="jira-to-ado", methods=["POST"])
def jira_to_ado(req: func.HttpRequest) -> func.HttpResponse:

    logging.info("─── Jira webhook received ───")

    # ── Step 1: Validate webhook secret ──────────────────────
    secret          = os.environ.get("JIRA_WEBHOOK_SECRET", "")
    received_secret = req.headers.get("X-Hub-Signature", "")

    if secret and received_secret:
        body_bytes = req.get_body()
        expected   = "sha256=" + hmac.new(
            secret.encode(), body_bytes, hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(expected, received_secret):
            logging.warning("❌ Invalid webhook secret — request rejected.")
            return func.HttpResponse("Unauthorized", status_code=401)

    # ── Step 2: Parse JSON payload ────────────────────────────
    try:
        payload = req.get_json()
    except ValueError:
        logging.error("❌ Invalid JSON payload received.")
        return func.HttpResponse("Invalid JSON", status_code=400)

    # ── Step 3: Extract fields ────────────────────────────────
    event       = payload.get("webhookEvent", "")
    issue       = payload.get("issue", {})
    fields      = issue.get("fields", {})

    jira_key    = issue.get("key", "UNKNOWN")
    summary     = fields.get("summary", "No Title")
    description = fields.get("description") or ""
    priority    = map_priority(
                    fields.get("priority", {}).get("name", "Medium")
                  )

    logging.info(f"Event    : {event}")
    logging.info(f"Jira Key : {jira_key}")
    logging.info(f"Summary  : {summary}")
    logging.info(f"Priority : {priority}")

    # ── Step 4: Route by event type ──────────────────────────
    if event == "jira:issue_created":
        logging.info("→ Routing to CREATE work item")
        resp = create_work_item(summary, description, priority, jira_key)

    elif event == "jira:issue_updated":
        logging.info("→ Routing to UPDATE work item")
        work_item_id = find_work_item(jira_key)

        if work_item_id:
            resp = update_work_item(work_item_id, summary, description, priority, jira_key)
        else:
            logging.warning(f"⚠️ No ADO work item found for {jira_key} — creating new.")
            resp = create_work_item(summary, description, priority, jira_key)

    else:
        logging.info(f"→ Event '{event}' not handled — ignoring.")
        return func.HttpResponse(
            json.dumps({"message": f"Event '{event}' ignored"}),
            status_code=200,
            mimetype="application/json"
        )

    # ── Step 5: Return response ───────────────────────────────
    return func.HttpResponse(
        json.dumps({
            "jira_key"  : jira_key,
            "event"     : event,
            "ado_status": resp.status_code
        }),
        status_code=200,
        mimetype="application/json"
    )
