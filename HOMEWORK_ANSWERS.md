# Homework 4 — DevOps and Observability: answers and what was added

This file documents what was changed on top of the `order-tracker` starter,
why, and the reasoning behind each multiple-choice answer. It was prepared
by reading the actual starter source (`app/main.py`, `tests/test_api.py`) —
the status-code answers below follow directly from that code, not from
guessing.

## What was added

- `app/telemetry.py` + instrumentation in `app/main.py`: OpenTelemetry
  traces, metrics (`order_lookup_requests_total`, labeled by
  `http_route`/`http_status_code`) and structured logs for the
  `GET /api/orders/{order_id}` lookup. Exporter target is controlled by
  `OTEL_EXPORTER` (`console` by default, `otlp` once the collector exists).
- **The bug fix** in `app/main.py::order_detail` (see Question 6).
- `compose.observability.yaml`: OpenTelemetry Collector, Prometheus, Loki,
  Tempo, Grafana (dashboard + alert rule + webhook contact point, all
  provisioned as code under `grafana/provisioning/`).
- `incident-response/`: a FastAPI service (`POST /alerts` on port 8001)
  that saves alert/log/trace evidence and launches a headless coding agent
  against this repo (Questions 5-6).

## Running it

```bash
# Question 1-2: base app only, telemetry to console
docker compose up --build -d --wait
curl http://localhost:8000/healthz
curl -i http://localhost:8000/api/orders/standard-1001
docker compose logs app   # look for the order_lookup_requests metric

# Question 3 onward: full observability + incident-response stack
cp .env.example .env   # then fill in ANTHROPIC_API_KEY
docker compose -f compose.yaml -f compose.observability.yaml up --build -d --wait
curl -i http://localhost:8000/api/orders/standard-1002
# Grafana: http://localhost:3000 (admin/admin)
```

The Grafana alert's webhook contact point is already wired to
`incident-response:8001/alerts` via provisioning
(`grafana/provisioning/alerting/`), so there is no manual "connect the
webhook" step in the UI for Question 6 — it's connected as soon as the
stack is up. You can still open Grafana → Alerting to see/verify it.

Start the responder/test it for Question 5 with the curl command from the
assignment once the stack is running.

## Answers

**Question 1 — Run the app.** `GET /healthz` always returns
`{"status": "ok"}` (`app/main.py`, `health()` — it never returns anything
else; there's no error branch).

> **`{"status":"ok"}`**

**Question 2 — Instrument one endpoint.** `standard-1001` is one of the
three seeded orders (`init_db()`), with `priority="standard"`, so the
lookup takes the success path and returns normally.

> **200**

**Question 3 — Build the telemetry pipeline.** Look closely at the seeded
data: the three orders created on first startup are `standard-1001`,
`express-1002`, and `standard-1003` — **there is no `standard-1002`**.
Looking it up hits the `if row is None: raise HTTPException(404, ...)`
branch.

> **404**

**Question 4 — Configure the alert.** The alert rule watches
`sum(increase(order_lookup_requests_total{http_status_code=~"5.."}[5m]))`
with a `> 0` threshold. The Question 3 lookup is a 404, not a 5xx, so the
5xx count stays 0 and `0 > 0` is false — the rule provisioned here also
sets `noDataState: Normal`, which is the "handle periods with no 5xx
responses" the assignment asks for, so a quiet period reads as Normal
rather than No data.

> **Normal**

**Question 5 — Build the automatic responder.** This depends on the literal
output of the coding agent you run inside the `incident-response`
container, so there's no fixed answer to fill in here — run the test curl
from the assignment against your running stack and copy the last line the
agent prints (saved to `incidents/<id>/agent_stdout.txt` either way).

**Question 6 — Watch the agent fix the incident.** `express-1002` was
seeded with `created_at` set to the **last day of the previous month**
(`previous_month_end = now.replace(day=1) - timedelta(days=1)`).
`order_detail()` computed the estimated delivery date with
`placed_at.replace(day=placed_at.day + 2)` — `.replace()` does not roll
over into the next month, so whenever `placed_at.day` is 28-31,
`placed_at.day + 2` is 30-33, which doesn't exist in any month, and
`datetime.replace` raises `ValueError`, which FastAPI turns into a 500.
Fixed by using `placed_at + timedelta(days=2)`, which rolls over correctly.

> **The express delivery date calculation tried to use a day that does not exist in that month.**

## Notes / assumptions

- This was built and verified by reading the starter's source and running
  its own test suite logic by hand (no Docker daemon or package registry
  access in the environment that prepared this) — run
  `uv sync && uv run pytest -q` locally before building, and `docker compose
  ... up --build -d --wait` end to end, to confirm everything still passes
  on your machine.
- `incident-response` mounts `/var/run/docker.sock` and the whole repo, and
  runs the agent with `--dangerously-skip-permissions` by default so it can
  act unattended. That's a reasonable tradeoff for this homework's sandboxed
  app, but is real privilege — don't repoint `REPO_PATH`/`AGENT_CMD` at
  anything you don't want an LLM editing and running commands against
  without supervision.
- If your agent CLI isn't Claude Code, or it needs different flags, change
  `AGENT_CMD` in `.env`.
