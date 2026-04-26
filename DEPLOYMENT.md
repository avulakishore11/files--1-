# Jira → Azure DevOps Sync — Deployment Guide

## Overview

This function app listens for Jira webhook events and syncs them to Azure DevOps (ADO) work items.

| Property | Value |
|---|---|
| Function App | `func-jira-ado-sync` |
| Resource Group | `kaseya-rg` |
| Plan | Flex Consumption (Linux) |
| Runtime | Python 3.11 |
| ADO Project | `kavula / Kaseya_infra` |
| Jira Instance | `https://kishore32780.atlassian.net` |

---

## Architecture

```
Jira Webhook POST
      │
      ▼
Azure Function (HTTP Trigger)
  func-jira-ado-sync/jira_to_ado
      │
      ├── jira:issue_created  →  Create ADO Work Item (Task)
      └── jira:issue_updated  →  Find existing item by tag → Update or Create
```

**What the function does:**
- Validates the incoming Jira webhook HMAC signature (`X-Hub-Signature`)
- Extracts issue key, summary, description, priority from the Jira payload
- Maps Jira priority (Highest/High/Medium/Low/Lowest) → ADO priority (1–4)
- Searches ADO for an existing work item tagged with the Jira key
- Creates or updates the ADO work item accordingly

---

## Repository Structure

```
.
├── function_app.py          # Main Azure Function (HTTP trigger)
├── host.json                # Functions host configuration
├── requirements.txt         # Python dependencies
├── azure-pipelines.yml      # ADO CI/CD pipeline
├── local.settings.json      # Local dev only — NEVER commit (gitignored)
└── .github/
    └── workflows/
        └── main_func-jira-ado-sync.yml  # GitHub Actions (not used — kept for reference)
```

---

## Prerequisites

### 1. Azure Resources

- **Function App**: `func-jira-ado-sync` on **Flex Consumption** plan
  - OS: Linux
  - Runtime: Python 3.11
  - Storage account: `kaseyarg86a2`

> **Why Flex Consumption?**
> Linux Consumption (Y1) deploys by uploading the zip directly from the pipeline
> agent to blob storage, requiring `Storage Blob Data Contributor` on the storage
> account. Flex Consumption routes the upload through the ARM management API using
> a temporary SAS token — only `Contributor` on the Function App is needed.
> Linux Consumption also reaches EOL September 30, 2028.

### 2. ADO Service Connection

- Name: `ado-2-azure`
- Type: Azure Resource Manager (service principal)
- Scope: subscription `bigk-appservices` or resource group `kaseya-rg`
- Required role: **Contributor** on the Function App (or resource group)

### 3. ADO Library — Variable Group

Go to **ADO → Pipelines → Library** and create a variable group named **`group`** with:

| Variable | Value | Secret |
|---|---|---|
| `ADO_PAT` | Azure DevOps Personal Access Token | Yes (lock it) |
| `JIRA_WEBHOOK_SECRET` | Jira webhook secret string | Yes (lock it) |

> After creating the variable group, go to **Pipeline permissions** tab inside the
> group and explicitly grant access to the pipeline. Without this, the pipeline is
> blocked even if the group is referenced in the YAML.

### 4. ADO Environment

Go to **ADO → Pipelines → Environments** and create an environment named **`production`**.
The deploy stage uses this for audit trail and optional approval gates.

---

## Azure Function App Settings

Set these in **Azure Portal → func-jira-ado-sync → Settings → Environment variables**:

| Name | Value | Notes |
|---|---|---|
| `FUNCTIONS_WORKER_RUNTIME` | `python` | Set by platform |
| `ADO_ORG` | `kavula` | Your ADO org name |
| `ADO_PROJECT` | `Kaseya_infra` | Your ADO project |
| `ADO_PAT` | *(PAT value)* | Injected by pipeline via variable group |
| `JIRA_WEBHOOK_SECRET` | *(secret)* | Injected by pipeline via variable group |
| `JIRA_BASE_URL` | `https://kishore32780.atlassian.net` | Your Jira instance |

> The pipeline's `AzureAppServiceSettings@1` step writes these automatically on
> every deployment. You only need to set them manually if running the function
> without the pipeline.

---

## CI/CD Pipeline — `azure-pipelines.yml`

The pipeline has two stages:

### Stage 1 — Build

1. Checks out the repository
2. Installs Python 3.11
3. Runs `pip install` targeting `.python_packages/lib/site-packages/`
   (Azure Functions on Linux reads packages from this path at runtime)
4. Zips all source files + installed packages into `release.zip`
   (excludes `.git`, `.venv`, `venv`, `local.settings.json`, `__pycache__`)
5. Publishes `release.zip` as a pipeline artifact named `drop`

### Stage 2 — Deploy

1. Downloads the `drop` artifact (auto-downloaded to `$(Pipeline.Workspace)/drop/`)
2. **`AzureFunctionApp@2`** — deploys `release.zip` to `func-jira-ado-sync`
   - On Flex Consumption, the task uses the ARM API with a temporary SAS token
   - No direct storage account access needed from the pipeline agent
3. **`AzureAppServiceSettings@1`** — writes all environment variables including
   `ADO_PAT` and `JIRA_WEBHOOK_SECRET` resolved from the `group` variable group

### Trigger

- Automatic on push to `main`
- Manual via **Run pipeline** in ADO

---

## Issues Encountered and Resolutions

### Issue 1 — `connectedServiceNameARM` invalid parameter
**Symptom:** Deploy step silently skipped, no functions deployed.
**Cause:** `AzureFunctionApp@2` uses `azureSubscription`, not `connectedServiceNameARM`
(that was the v1 parameter name).
**Fix:** Renamed the input to `azureSubscription`.

---

### Issue 2 — Variable group not linked, secrets written as literal strings
**Symptom:** Azure portal showed `ADO_PAT` = `$(ADO_PAT)` as a literal string.
**Cause:** The pipeline YAML referenced `$(ADO_PAT)` but never declared
`- group: group` in the `variables` section. ADO treated them as undefined variables.
**Fix:** Added `- group: group` at the top of the `variables` block using the
list format (required when mixing groups and named variables).

```yaml
variables:
  - group: group          # ← this line was missing
  - name: azureServiceConnection
    value: 'ado-2-azure'
```

Also required: grant the pipeline **Pipeline permissions** access inside the
Library variable group, otherwise the pipeline is blocked from reading it.

---

### Issue 3 — `runtimeStack` invalid, wrong artifact path in deployment job
**Symptom:** Deploy task errored or behaved unexpectedly.
**Cause 1:** `runtimeStack: 'PYTHON|3.11'` is not a valid input for `AzureFunctionApp@2`.
**Cause 2:** Inside a `deployment` job, artifacts are auto-downloaded to
`$(Pipeline.Workspace)/<ArtifactName>/`, not `$(System.ArtifactsDirectory)`.
Using a manual `DownloadBuildArtifacts@1` step pointed to the wrong path.
**Fix:** Removed `runtimeStack`, removed the manual download step, updated the
package path to `$(Pipeline.Workspace)/drop/release.zip`.

---

### Issue 4 — `StorageError: Forbidden` (root cause of all deployment failures)
**Symptom:** Deploy step failed with `StorageError: Forbidden` immediately after
"The Deployment Type option does not apply for Linux Consumption."
**Cause:** Linux Consumption (Y1) plan ignores `deploymentMethod: 'zipDeploy'` and
instead uploads the zip **directly from the pipeline agent** to the blob storage
account (`stjiraadosynckaseya`). The service principal behind `ado-2-azure` did not
have `Storage Blob Data Contributor` on that storage account. Additionally, the
storage account had network restrictions blocking Microsoft-hosted agent IPs.
**Fix:** Migrated from Linux Consumption to **Flex Consumption** plan. On Flex
Consumption, `AzureFunctionApp@2` routes the upload through the ARM management API
with a temporary SAS token — the pipeline agent never touches the storage account
directly. Only `Contributor` on the Function App is required.

---

### Issue 5 — Dependencies missing from zip (functions not discovered)
**Symptom:** Deployment succeeded but the Azure portal showed "Create functions in
your preferred environment" — no functions listed.
**Cause:** The zip contained only `function_app.py`, `host.json`, and
`requirements.txt`. Because Flex Consumption runs the app directly from the zip
(WEBSITE_RUN_FROM_PACKAGE), Oryx does not install packages at runtime. The zip
must include the installed packages.
**Fix:** Added a `pip install --target=".python_packages/lib/site-packages"` step
before zipping. The `.python_packages` folder is automatically added to `sys.path`
by the Azure Functions Python worker.

---

## Local Development

Create `local.settings.json` (already gitignored):

```json
{
  "IsEncrypted": false,
  "Values": {
    "AzureWebJobsStorage"      : "UseDevelopmentStorage=true",
    "FUNCTIONS_WORKER_RUNTIME" : "python",
    "ADO_ORG"                  : "kavula",
    "ADO_PROJECT"              : "Kaseya_infra",
    "ADO_PAT"                  : "<your-pat>",
    "JIRA_WEBHOOK_SECRET"      : "<your-secret>",
    "JIRA_BASE_URL"            : "https://kishore32780.atlassian.net"
  }
}
```

Run locally:
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
func start
```

Test with curl:
```bash
curl -X POST http://localhost:7071/api/jira_to_ado \
  -H "Content-Type: application/json" \
  -d '{
    "webhookEvent": "jira:issue_created",
    "issue": {
      "key": "TEST-1",
      "fields": {
        "summary": "Test issue",
        "description": "Test description",
        "priority": { "name": "High" }
      }
    }
  }'
```

---

## Jira Webhook Configuration

In Jira: **Settings → System → WebHooks → Create a WebHook**

| Field | Value |
|---|---|
| URL | `https://func-jira-ado-sync.azurewebsites.net/api/jira_to_ado` |
| Events | Issue created, Issue updated |
| Secret | Value stored in `JIRA_WEBHOOK_SECRET` |

---

## Security Notes

- `local.settings.json` is gitignored — never commit it
- `ADO_PAT` and `JIRA_WEBHOOK_SECRET` are stored as **locked secret variables**
  in the ADO Library variable group — never hardcode them in YAML
- The function validates the `X-Hub-Signature` HMAC header on every incoming
  request — unauthenticated requests are rejected with HTTP 401
- Rotate the ADO PAT before its expiry date and update the variable group
