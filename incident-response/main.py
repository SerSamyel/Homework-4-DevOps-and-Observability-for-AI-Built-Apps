"""Incident responder for Order Tracker (Homework 4, Questions 5-6).

Receives Grafana alert webhooks at POST /alerts, saves the evidence needed
to understand the problem (the alert payload, recent logs from Loki, recent
error traces from Tempo), then starts a headless coding agent against the
mounted repository so it can investigate, fix the bug, rebuild/restart the
app, and verify the fix.

This is a homework-grade prototype, not a production design: it runs the
agent with permission checks skipped (AGENT_CMD defaults to
`claude -p --dangerously-skip-permissions`) and the container has access to
the host's Docker socket and the full repository. Treat it accordingly --
don't point AGENT_CMD/REPO_PATH at anything you don't trust an LLM to edit
unsupervised.
"""

import json
import logging
import os
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("incident-response")

LOKI_URL = os.getenv("LOKI_URL", "http://loki:3100")
TEMPO_URL = os.getenv("TEMPO_URL", "http://tempo:3200")
REPO_PATH = Path(os.getenv("REPO_PATH", "/workspace"))
INCIDENTS_DIR = Path(os.getenv("INCIDENTS_DIR", "/workspace/incidents"))
SERVICE_NAME = os.getenv("TELEMETRY_SERVICE_NAME", "order-tracker")
AGENT_CMD = os.getenv("AGENT_CMD", "claude -p --dangerously-skip-permissions")
AGENT_TIMEOUT_SECONDS = int(os.getenv("AGENT_TIMEOUT_SECONDS", "900"))
COMPOSE_FILES = os.getenv(
    "COMPOSE_FILES", "-f compose.yaml -f compose.observability.yaml"
)
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://app:8000")

app = FastAPI(title="Order Tracker Incident Responder")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


def _fetch_recent_logs(minutes: int = 15, limit: int = 300):
    """Best-effort pull of recent app logs from Loki."""
    end_ns = time.time_ns()
    start_ns = end_ns - minutes * 60 * 1_000_000_000
    query = f'{{service_name="{SERVICE_NAME}"}}'
    url = f"{LOKI_URL}/loki/api/v1/query_range"
    params = {"query": query, "start": start_ns, "end": end_ns, "limit": limit, "direction": "backward"}
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        lines = []
        for stream in data.get("data", {}).get("result", []):
            labels = stream.get("stream", {})
            for ts, line in stream.get("values", []):
                lines.append({"timestamp_ns": ts, "labels": labels, "line": line})
        lines.sort(key=lambda e: e["timestamp_ns"])
        return lines
    except Exception as exc:  # noqa: BLE001 - this is best-effort evidence gathering
        logger.warning("could not fetch logs from Loki: %s", exc)
        return {"error": str(exc)}


def _fetch_recent_error_traces(minutes: int = 15, limit: int = 10):
    """Best-effort pull of recent error traces from Tempo."""
    end_s = int(time.time())
    start_s = end_s - minutes * 60
    traceql = f'{{resource.service.name="{SERVICE_NAME}" && status=error}}'
    search_url = f"{TEMPO_URL}/api/search"
    params = {"q": traceql, "start": start_s, "end": end_s, "limit": limit}
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(search_url, params=params)
            resp.raise_for_status()
            traces_summary = resp.json().get("traces", [])

            detailed = []
            for summary in traces_summary[:5]:
                trace_id = summary.get("traceID")
                if not trace_id:
                    continue
                try:
                    trace_resp = client.get(f"{TEMPO_URL}/api/traces/{trace_id}")
                    trace_resp.raise_for_status()
                    detailed.append({"traceID": trace_id, "trace": trace_resp.json()})
                except Exception as exc:  # noqa: BLE001
                    detailed.append({"traceID": trace_id, "error": str(exc)})
            return {"summary": traces_summary, "detailed": detailed}
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not fetch traces from Tempo: %s", exc)
        return {"error": str(exc)}


def _build_prompt(alert: dict, incident_dir: Path, is_test: bool) -> str:
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})

    if is_test:
        return (
            "You are the automatic incident responder for the Order Tracker app.\n"
            "This is a TEST alert (label test=true) sent to verify the responder "
            "wiring end to end -- there is no real incident.\n\n"
            f"Alert payload:\n{json.dumps(alert, indent=2)}\n\n"
            "Do not investigate anything and do not change any files. "
            "Reply with a single short line confirming you received the test "
            "alert and are ready to handle real incidents."
        )

    return (
        "You are the automatic incident responder for the Order Tracker app, "
        "a FastAPI service at the root of this repository (working directory).\n\n"
        "A Grafana alert fired for 5xx responses on an order lookup. Investigate "
        "and fix the root cause using the evidence below, then verify the fix.\n\n"
        f"Alert name: {labels.get('alertname', 'unknown')}\n"
        f"Labels: {json.dumps(labels)}\n"
        f"Summary: {annotations.get('summary', '')}\n"
        f"Description: {annotations.get('description', '')}\n\n"
        f"Saved evidence for this incident is in: {incident_dir}\n"
        f"  - alert.json    the raw Grafana alert payload\n"
        f"  - logs.json     recent app logs pulled from Loki\n"
        f"  - traces.json   recent error traces pulled from Tempo\n\n"
        "Steps:\n"
        "1. Read the saved logs and traces to find the failing request and the "
        "root cause in the application source under app/.\n"
        "2. Fix the bug in the source code. Keep the fix minimal and targeted.\n"
        "3. Run the test suite: `uv run pytest -q` (run `uv sync` first if needed).\n"
        f"4. Rebuild and restart the app service: "
        f"`docker compose {COMPOSE_FILES} up -d --build app`.\n"
        f"5. Verify the fix by re-requesting the endpoint that failed "
        f"(the app is reachable at {APP_BASE_URL} from inside this container, "
        "or http://localhost:8000 from the host) and confirming it no longer "
        "returns a 5xx.\n"
        "6. Reply with a short summary: what was broken, what you changed, and "
        "confirmation that the endpoint now works."
    )


def _run_agent(prompt: str, incident_dir: Path):
    argv = shlex.split(AGENT_CMD)
    logger.info("starting headless agent: %s", AGENT_CMD)
    try:
        result = subprocess.run(
            argv,
            input=prompt,
            cwd=str(REPO_PATH),
            capture_output=True,
            text=True,
            timeout=AGENT_TIMEOUT_SECONDS,
        )
        stdout, stderr, returncode = result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = (exc.stderr or "") + f"\n[responder] agent timed out after {AGENT_TIMEOUT_SECONDS}s"
        returncode = None
    except FileNotFoundError as exc:
        stdout = ""
        stderr = f"[responder] could not start agent command {AGENT_CMD!r}: {exc}"
        returncode = None

    (incident_dir / "agent_stdout.txt").write_text(stdout, encoding="utf-8")
    (incident_dir / "agent_stderr.txt").write_text(stderr, encoding="utf-8")

    last_line = ""
    for line in reversed(stdout.splitlines()):
        if line.strip():
            last_line = line.strip()
            break

    return {"returncode": returncode, "last_line": last_line, "stdout": stdout, "stderr": stderr}


@app.post("/alerts")
async def receive_alert(request: Request):
    payload = await request.json()
    alerts = payload.get("alerts", [])
    if not alerts:
        logger.warning("received webhook with no alerts: %s", payload)
        return {"handled": 0, "results": []}

    results = []
    for alert in alerts:
        if alert.get("status") == "resolved":
            logger.info("skipping resolved alert: %s", alert.get("labels"))
            continue

        labels = alert.get("labels", {})
        is_test = str(labels.get("test", "")).lower() == "true"

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        alertname = labels.get("alertname", "alert")
        incident_id = f"{timestamp}-{alertname}"
        incident_dir = INCIDENTS_DIR / incident_id
        incident_dir.mkdir(parents=True, exist_ok=True)

        (incident_dir / "alert.json").write_text(json.dumps(alert, indent=2), encoding="utf-8")

        if not is_test:
            logs = _fetch_recent_logs()
            (incident_dir / "logs.json").write_text(json.dumps(logs, indent=2), encoding="utf-8")

            traces = _fetch_recent_error_traces()
            (incident_dir / "traces.json").write_text(json.dumps(traces, indent=2), encoding="utf-8")

        prompt = _build_prompt(alert, incident_dir, is_test)
        (incident_dir / "prompt.md").write_text(prompt, encoding="utf-8")

        agent_result = _run_agent(prompt, incident_dir)
        logger.info("incident %s: agent last line: %s", incident_id, agent_result["last_line"])

        results.append(
            {
                "incident_id": incident_id,
                "is_test": is_test,
                "agent_returncode": agent_result["returncode"],
                "agent_last_line": agent_result["last_line"],
            }
        )

    return {"handled": len(results), "results": results}
