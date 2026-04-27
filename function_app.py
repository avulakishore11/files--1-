import azure.functions as func
import requests
import json
import base64
import os
import logging
import hmac
import hashlib

app = func.FunctionApp()

ADO_TIMEOUT = 30  # seconds for all outbound ADO API calls


def map_priority(jira_priority: str) -> int:
    mapping = {
        "Highest": 1,
        "High":    1,
        "Medium":  2,
        "Low":     3,
        "Lowest":  4,
    }
    return mapping.get(jira_priority, 2)


def get_ado_headers() -> dict:
    pat = os.environ.get("ADO_PAT")
    if not pat:
        raise EnvironmentError("ADO_PAT environment variable is not set.")
    token = base64.b64encode(f":{pat}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type":  "application/json-patch+json",
    }


def extract_description(raw) -> str:
    """Handles both plain-text and Jira Cloud ADF (Atlassian Document Format)."""
    if not raw:
        return ""
    if isinstance(raw, dict):
        # ADF: flatten paragraph text nodes into plain text
        lines = []
        for block in raw.get("content", []):
            parts = []
            for node in block.get("content", []):
                if node.get("type") == "text":
                    parts.append(node.get("text", ""))
            if parts:
                lines.append("".join(parts))
        return "\n".join(lines)
    return str(raw)


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

    try:
        resp = requests.post(wiql_url, headers=get_ado_headers(),
                             json=query, timeout=ADO_TIMEOUT)
    except requests.Timeout:
        logging.error("Timeout querying ADO for work item.")
        return None

    if resp.status_code != 200:
        logging.error(f"WIQL query failed — HTTP {resp.status_code}: {resp.text}")
        return None

    items = resp.json().get("workItems", [])
    return items[0]["id"] if items else None


def create_work_item(summary: str, description: str, priority: int, jira_key: str):
    org     = os.environ["ADO_ORG"]
    project = os.environ["ADO_PROJECT"]

    url = (
        f"https://dev.azure.com/{org}/{project}"
        f"/_apis/wit/workitems/$Task?api-version=7.1"
    )
    body = [
        {"op": "add", "path": "/fields/System.Title",
         "value": f"[{jira_key}] {summary}"},
        {"op": "add", "path": "/fields/System.Description",
         "value": description},
        {"op": "add", "path": "/fields/Microsoft.VSTS.Common.Priority",
         "value": priority},
        {"op": "add", "path": "/fields/System.Tags",
         "value": jira_key},
    ]

    try:
        resp = requests.post(url, headers=get_ado_headers(),
                             json=body, timeout=ADO_TIMEOUT)
    except requests.Timeout:
        logging.error("Timeout creating ADO work item.")
        raise

    if resp.status_code not in (200, 201):
        logging.error(f"CREATE failed — HTTP {resp.status_code}: {resp.text}")
    else:
        logging.info(f"CREATE → ADO HTTP {resp.status_code} | Jira Key: {jira_key}")

    return resp


def update_work_item(work_item_id: int, summary: str, description: str,
                     priority: int, jira_key: str):
    org = os.environ["ADO_ORG"]

    url = (
        f"https://dev.azure.com/{org}"
        f"/_apis/wit/workitems/{work_item_id}?api-version=7.1"
    )
    body = [
        {"op": "replace", "path": "/fields/System.Title",
         "value": f"[{jira_key}] {summary}"},
        {"op": "replace", "path": "/fields/System.Description",
         "value": description},
        {"op": "replace", "path": "/fields/Microsoft.VSTS.Common.Priority",
         "value": priority},
    ]

    try:
        resp = requests.patch(url, headers=get_ado_headers(),
                              json=body, timeout=ADO_TIMEOUT)
    except requests.Timeout:
        logging.error("Timeout updating ADO work item.")
        raise

    if resp.status_code not in (200, 201):
        logging.error(f"UPDATE failed — HTTP {resp.status_code}: {resp.text}")
    else:
        logging.info(f"UPDATE → ADO HTTP {resp.status_code} | Work Item ID: {work_item_id}")

    return resp


@app.route(route="jira_to_ado", methods=["POST"])
def jira_to_ado(req: func.HttpRequest) -> func.HttpResponse:

    logging.info("─── Jira webhook received ───")

    # ── Step 1: Validate webhook secret ──────────────────────────────────────
    secret = os.environ.get("JIRA_WEBHOOK_SECRET", "")
    if secret:
        received_sig = req.headers.get("X-Hub-Signature", "")
        if not received_sig:
            logging.warning("Request rejected — X-Hub-Signature header missing.")
            return func.HttpResponse("Unauthorized", status_code=401)

        body_bytes = req.get_body()
        expected   = "sha256=" + hmac.new(
            secret.encode(), body_bytes, hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(expected, received_sig):
            logging.warning("Request rejected — invalid webhook signature.")
            return func.HttpResponse("Unauthorized", status_code=401)

    # ── Step 2: Parse JSON payload ────────────────────────────────────────────
    try:
        payload = req.get_json()
    except ValueError:
        logging.error("Invalid JSON payload received.")
        return func.HttpResponse("Invalid JSON", status_code=400)

    # ── Step 3: Extract fields ────────────────────────────────────────────────
    event    = payload.get("webhookEvent", "")
    issue    = payload.get("issue", {})
    fields   = issue.get("fields", {})

    jira_key    = issue.get("key", "UNKNOWN")
    summary     = fields.get("summary", "No Title")
    description = extract_description(fields.get("description"))
    priority    = map_priority(fields.get("priority", {}).get("name", "Medium"))

    logging.info(f"Event    : {event}")
    logging.info(f"Jira Key : {jira_key}")
    logging.info(f"Summary  : {summary}")
    logging.info(f"Priority : {priority}")

    # ── Step 4: Route by event type ───────────────────────────────────────────
    try:
        if event == "jira:issue_created":
            logging.info("→ Routing to CREATE work item")
            resp = create_work_item(summary, description, priority, jira_key)

        elif event == "jira:issue_updated":
            logging.info("→ Routing to UPDATE work item")
            work_item_id = find_work_item(jira_key)

            if work_item_id:
                resp = update_work_item(work_item_id, summary, description,
                                        priority, jira_key)
            else:
                logging.warning(f"No ADO work item found for {jira_key} — creating new.")
                resp = create_work_item(summary, description, priority, jira_key)

        else:
            logging.info(f"Event '{event}' not handled — ignoring.")
            return func.HttpResponse(
                json.dumps({"message": f"Event '{event}' ignored"}),
                status_code=200,
                mimetype="application/json",
            )

    except requests.Timeout:
        return func.HttpResponse("ADO API timeout", status_code=504)
    except EnvironmentError as e:
        logging.error(str(e))
        return func.HttpResponse("Server misconfiguration", status_code=500)

    # ── Step 5: Return response ───────────────────────────────────────────────
    ado_ok = resp.status_code in (200, 201)
    return func.HttpResponse(
        json.dumps({
            "jira_key":   jira_key,
            "event":      event,
            "ado_status": resp.status_code,
            "success":    ado_ok,
        }),
        status_code=200 if ado_ok else 502,
        mimetype="application/json",
    )
