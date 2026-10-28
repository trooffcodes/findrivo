# FindRivo

FindRivo is a Flask web app and CLI for autonomous B2B lead discovery, website auditing, contact extraction, and AI-assisted outreach hooks. It discovers local businesses through Google Maps, audits their websites, enriches contacts, and streams qualified leads to the browser or a CSV export.

## Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) for dependency management
- Chromium binaries for Playwright
- At least one Gemini or Groq API key for AI features. The pipeline has deterministic fallbacks but AI quality is reduced without keys.

## Installation

```bash
uv sync
uv run playwright install chromium
```

`uv sync` installs the project in editable mode and provides the `findrivo` console script.

On Linux servers you may also need the Playwright system dependencies:

```bash
uv run playwright install --with-deps chromium
```

## Configuration

Copy the example environment file and fill in your credentials:

```bash
cp .env.example src/.env
```

The app loads `src/.env` regardless of the current working directory. Environment variables already present in the process take precedence.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `FINDRIVO_ENV` | Production | `development` | Set to `production` to enable fail-fast safety checks. |
| `DEBUG` | No | `false` | Flask debug mode. Must be `false` in production. |
| `FLASK_PORT` | No | `2111` | Local Flask port. |
| `FINDRIVO_API_TOKEN` | Production | empty | Bearer/X-API-Key token required for API calls. |
| `ALLOW_UNAUTHENTICATED` | No | `DEBUG` value | Allows API use without a token. Only use for local development. |
| `MAX_ACTIVE_JOBS` | No | `2` | Maximum concurrent pipeline jobs. |
| `RATE_LIMIT_REQUESTS` | No | `20` | Requests allowed per client IP per window. `0` disables. |
| `RATE_LIMIT_WINDOW_SECONDS` | No | `60` | Rate-limit window. |
| `MAX_TARGET_COUNT` | No | `25` | Maximum `target_count` accepted by the API. |
| `GEMINI_API_KEY` | No | empty | Google Gemini API key. |
| `GROQ_API_KEY_1/2/3` | No | empty | Groq keys for fallback and vision. |
| `GEMINI_MODEL` | No | `gemini-3.5-flash-lite` | Gemini model ID. |
| `GROQ_FALLBACK_MODEL` | No | `openai/gpt-oss-120b` | Groq text model. |
| `QWEN_VISION_MODEL` | No | `qwen/qwen3.8-27b` | Groq vision model. |
| `LLM_TIMEOUT_SECONDS` | No | `45` | Per-provider request timeout. |
| `REQUEST_TIMEOUT` | No | `8` | Website fetch timeout. |
| `LINK_CHECK_TIMEOUT` | No | `3.5` | Internal link-check timeout. |
| `HEADLESS` | No | `true` | Run Chromium headless. |
| `CONCURRENCY` | No | `2` | Discovery browser workers. |
| `MAX_LEADS` | No | `20` | Maximum listing URLs collected per search query. |
| `PLAYWRIGHT_NO_SANDBOX` | No | `false` | Disables the Chromium sandbox. Unsafe; only for constrained containers. |
| `RESUME_DISCOVERY` | No | `false` | Reuse and persist `PROGRESS_FILE`. Off by default to prevent stale progress from suppressing leads. |
| `FINDRIVO_RESULTS_DIR` | No | `data/jobs` | Local directory for job result snapshots (page-reload recovery). |
| `RESULTS_RETENTION_HOURS` | No | `24` | How long saved lead snapshots are kept before automatic pruning. |
| `SHUTDOWN_GRACE_SECONDS` | No | `12` | Time active jobs get to finish after Ctrl+C before forced exit. |
| `BROWSER_FALLBACK_ENABLED` | No | `true` | Allow optional Playwright fallback for promising leads with missing contacts. |
| `BROWSER_FALLBACK_TIMEOUT` | No | `15` | Per-page timeout for the browser fallback render. |
| `BROWSER_FALLBACK_MAX_PER_JOB` | No | `15` | Cap on renders per job (`0` disables). |
| `BROWSER_FALLBACK_RETRY_COOLDOWN` | No | `900` | Seconds before a failed host may be re-rendered. |
| `FINDRIVO_LOG_DIR` | No | `logs` | Log directory, resolved against the repository root. |
| `FINDRIVO_EXPORT_DIR` | No | `exports` | CSV export directory. |
| `PROGRESS_FILE` | No | `discovery_progress.json` | Discovery progress file when resume is enabled. |

Generate a production token:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Local Development

Start the web interface:

```bash
uv run findrivo
```

Or, without relying on the installed console script:

```bash
PYTHONPATH=src uv run python src/app.py
```

Then open <http://localhost:2111>. If `FINDRIVO_API_TOKEN` is set, enter it in the API Token field before starting a campaign.

Run diagnostics and the CLI:

```bash
uv run findrivo --doctor
uv run findrivo --cli --query "Dental Clinic in Austin, TX" --count 5
```

Run the unit tests:

```bash
PYTHONPATH=src uv run python -m unittest discover -s tests -v
PYTHONPATH=src uv run python -m unittest discover -s src/tests -v
```

Run lint:

```bash
uv run ruff check src tests
```

## HTTP API

All API endpoints except `/` and `/api/health` require authentication when `FINDRIVO_API_TOKEN` is configured. Supply the master token with `X-API-Key` or `Authorization: Bearer ...`. `POST /api/pipeline/start` returns a short-lived per-job capability token; pass it as the `token` query parameter to the SSE stream and CSV download links. The master token is never placed in URLs.

- `POST /api/pipeline/start` — `{"query": "...", "agency_services": "...", "target_count": 5, "only_redesign": true}`
- `GET /api/pipeline/stream/<task_id>` — Server-Sent Events stream.
- `GET /api/results/latest` — Most recent job snapshot (status + collected leads), used by the UI to restore state after a page reload. Requires no snapshot token.
- `GET /api/results/<task_id>` — A specific job snapshot.
- `GET /api/download/<filename>` — Download a generated CSV from the export directory.
- `POST /api/maintenance/clear` — Clear-start deletion: `{"mode": "data"|"logs"|"all", "confirm": true}`. Requires authentication when configured. Refused with HTTP 409 while a job is still running (that job is actively writing data). `mode: "data"` removes job snapshots and discovery progress but never your exports; `mode: "logs"` truncates log files (and backups) in place; `mode: "all"` does both. The UI exposes these as the three Maintenance buttons with confirmation dialogs.
- `GET /api/health` — Liveness, provider circuit state, and snapshot retention info.

## Result persistence and shutdown

Qualified leads are streamed to the CSV immediately and also written to a small
JSON snapshot under `FINDRIVO_RESULTS_DIR` (one file per job, pruned after
`RESULTS_RETENTION_HOURS`). Reloading the page restores the current or most
recent job with its status (`running`, `interrupted`, `complete`, `failed`);
duplicates are filtered when reconnecting. This is local-disk persistence for
self-hosted deployments — on an ephemeral hosting platform the snapshots live
and die with the container, so treat them as a cache, not a database.

Ctrl+C (or SIGTERM) prints `Shutdown requested. Closing workers and browser
resources...`, stops scheduling new work, lets active tasks drain within
`SHUTDOWN_GRACE_SECONDS`, closes Playwright resources, and exits with code 130.
Already collected leads are preserved in both the CSV and the snapshot. A
second Ctrl+C exits immediately; repeated interrupts during cleanup never
raise uncontrolled errors.

## Scoring, evidence, and response-time honesty

Each lead carries four separated values instead of one confidence meter:

- `extraction_confidence` / `confidence_level` — evidence quality behind the
  extracted facts (verified / unverified / inferred / conflicting / missing
  per field; missing data never raises confidence and 100 is impossible).
- `opportunity_score` + `opportunity_reasons` — how promising the business is
  as a redesign prospect, credited only for verifiable criteria (verified
  absence of any official website, directly observed technical defects, or a
  reliably measured slow response). A missing favicon or alt text alone never
  qualifies a lead.
- `audit_defects` — directly observed website findings (unchanged).
- `outreach_suitability` — `ready`, `needs_verification`, or `insufficient`.

The website timing reported is a client-side HTTP GET duration (DNS, TLS, and
network included), summarised over two samples when possible and labelled
`inconclusive` / rejected from scoring when the measurement cannot be trusted.
It is explicitly not a browser page-load metric.

## Production Deployment

FindRivo runs an in-process worker pool and Playwright browser. Use **one process with multiple threads**, not multiple processes, unless you add shared state/queueing. A simple WSGI deployment:

```bash
FINDRIVO_ENV=production \
DEBUG=false \
ALLOW_UNAUTHENTICATED=false \
FINDRIVO_API_TOKEN="<long-random-token>" \
PYTHONPATH=src \
gunicorn --workers 1 --threads 8 --timeout 300 --bind 0.0.0.0:2111 wsgi:app
```

`gunicorn` is not included as a project dependency. Install it in the deployment image if you use this command, or use another WSGI server. Do not use `flask run` or the Werkzeug development server in production.

Production checklist:

- `FINDRIVO_ENV=production`, `DEBUG=false`, and `ALLOW_UNAUTHENTICATED=false`.
- `FINDRIVO_API_TOKEN` set to a long random value and delivered via your secret manager.
- TLS terminated at a reverse proxy; do not expose the WSGI port directly.
- Egress firewall restricting the app to public internet destinations. The app blocks private/loopback URLs at the application layer, but network-level egress control is still required.
- Chromium sandbox enabled. Only set `PLAYWRIGHT_NO_SANDBOX=true` if the container cannot support a sandbox.
- Writable `FINDRIVO_LOG_DIR` and `FINDRIVO_EXPORT_DIR`.
- Logs and exports excluded from source control. They are already ignored by `.gitignore`.
- Backups/retention policy for exported leads and discovery progress. Lead data may contain personal contact information.

## Known Operational Risks

- Google Maps DOM scraping is inherently fragile and may be blocked or changed by Google.
- LLM providers can be rate-limited or unavailable; deterministic fallbacks keep the pipeline running but reduce output quality.
- The in-memory job registry and rate limiter are per-process and reset on restart.
- Per-job SSE/download capability tokens expire with the in-memory stream entry (`STREAM_TTL_SECONDS`, default one hour). The master API token continues to authorize downloads while the file exists.
- Rate limiting uses the direct client address. Behind a reverse proxy, all requests may appear to come from the proxy unless you add trusted-proxy handling at the proxy or application layer. Do not blindly trust `X-Forwarded-For`.
- The dashboard loads Tailwind CSS from a CDN. Self-host frontend assets and tighten the Content-Security-Policy if your deployment forbids third-party scripts.
- CSV files are written with formula-injection neutralization, but review exported data before opening in spreadsheet software.
- This repository does not include a database, authentication user system, or multi-tenant isolation.
