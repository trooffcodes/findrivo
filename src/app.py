"""
app.py
FindRivo Autonomous Lead Generation Engine — Web App & Interactive CLI.
Features:
  - Real-time Server-Sent Events (SSE) streaming web UI
  - Interactive CLI with colored diagnostics (--doctor)
  - Production REST API
"""

import hmac
import json
import queue
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from functools import wraps
from pathlib import Path
from flask import Flask, Response, jsonify, render_template_string, request, send_file
from werkzeug.utils import secure_filename

from config import Config
from core import maintenance
from core import persistence
from core import shutdown as shutdown_core
from core.logger import app_logger
from core.pipeline import LeadGenPipeline
from methods.exporter import build_export_filename

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
pipeline = LeadGenPipeline()

# A 'running' snapshot cannot be live at startup: if one survived a restart,
# the process died mid-job. Correct the state so the UI shows partial
# results instead of an endless spinner.
try:
    persistence.mark_running_as_interrupted()
except Exception as startup_err:  # never block startup on snapshot repair
    app_logger.warning("Startup snapshot repair failed: %s", startup_err)

ACTIVE_STREAMS: dict = {}
_ACTIVE_STREAMS_LOCK = threading.Lock()
_JOBS_SEMAPHORE = threading.BoundedSemaphore(Config.MAX_ACTIVE_JOBS)
_RATE_LOCK = threading.Lock()
_RATE_BUCKETS: dict = defaultdict(deque)


def _request_api_token() -> str:
    token = request.headers.get("X-API-Key", "").strip()
    if token:
        return token
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return ""


def _auth_failure_response():
    return jsonify({"error": "Authentication required"}), 401


def _master_auth_valid() -> bool:
    expected = Config.API_TOKEN
    if expected:
        supplied = _request_api_token()
        return bool(supplied) and hmac.compare_digest(supplied, expected)
    return Config.ALLOW_UNAUTHENTICATED


def _capability_valid(entry: dict) -> bool:
    supplied = request.args.get("token", "").strip()
    expected = entry.get("token", "")
    return bool(supplied and expected and hmac.compare_digest(supplied, expected))


def require_api_auth(view):
    """Require FINDRIVO_API_TOKEN unless unauthenticated access is explicitly allowed."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if _master_auth_valid():
            return view(*args, **kwargs)
        if Config.API_TOKEN:
            return _auth_failure_response()
        return jsonify({"error": "Server is not configured with an API token"}), 503
    return wrapped


def _rate_limit_exceeded() -> bool:
    limit = Config.RATE_LIMIT_REQUESTS
    if limit <= 0:
        return False
    key = request.remote_addr or "unknown"
    now = time.monotonic()
    cutoff = now - Config.RATE_LIMIT_WINDOW_SECONDS
    with _RATE_LOCK:
        if len(_RATE_BUCKETS) > 4096:
            for stale_key in [
                bucket_key
                for bucket_key, times in _RATE_BUCKETS.items()
                if not times or times[-1] < cutoff
            ]:
                _RATE_BUCKETS.pop(stale_key, None)
        bucket = _RATE_BUCKETS[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            return True
        bucket.append(now)
    return False


def rate_limited(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if _rate_limit_exceeded():
            return jsonify({"error": "Rate limit exceeded"}), 429
        return view(*args, **kwargs)
    return wrapped


def _parse_target_count(raw) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as err:
        raise ValueError("target_count must be an integer") from err
    if value < 1 or value > Config.MAX_TARGET_COUNT:
        raise ValueError(f"target_count must be between 1 and {Config.MAX_TARGET_COUNT}")
    return value


def _safe_export_path(filename: str):
    """Resolve a download name inside the export directory, or return None."""
    if not filename or filename != Path(filename).name:
        return None
    cleaned = secure_filename(filename)
    if not cleaned or cleaned != filename or not cleaned.lower().endswith(".csv"):
        return None
    export_dir = Config.EXPORT_DIR.resolve()
    candidate = (export_dir / cleaned).resolve()
    try:
        candidate.relative_to(export_dir)
    except ValueError:
        return None
    return candidate


class _JobRecorder:
    """Keeps the persistent snapshot of one job in sync with live results."""

    def __init__(self, task_id: str, query: str, agency_services, target_count: int, csv_filename: str):
        self._lock = threading.Lock()
        self.task_id = task_id
        self.state = {
            "task_id": task_id,
            "status": persistence.STATUS_RUNNING,
            "query": query,
            "agency_services": agency_services or "",
            "target_count": target_count,
            "csv_filename": csv_filename,
            "leads": [],
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self._flush()

    def _flush(self) -> None:
        persistence.save_job(self.state)

    def add_lead(self, lead: dict) -> None:
        with self._lock:
            known = {persistence.lead_identity(l) for l in self.state["leads"]}
            key = persistence.lead_identity(lead)
            if key not in known:
                self.state["leads"].append(lead)
            self._flush()

    def mark(self, status: str) -> None:
        with self._lock:
            self.state["status"] = status
            self._flush()

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.state)


def _cleanup_streams() -> None:
    cutoff = time.time() - Config.STREAM_TTL_SECONDS
    with _ACTIVE_STREAMS_LOCK:
        for task_id, entry in list(ACTIVE_STREAMS.items()):
            if entry.get("created", 0) < cutoff:
                entry.get("cancel_event", threading.Event()).set()
                ACTIVE_STREAMS.pop(task_id, None)
        # Prune expired persisted snapshots opportunistically.
        try:
            persistence.prune_expired()
        except Exception as err:
            app_logger.warning("Snapshot pruning failed: %s", err)


def validate_runtime_config() -> None:
    """Fail fast on obviously unsafe production configuration."""
    if not Config.IS_PRODUCTION:
        return
    problems = []
    if Config.DEBUG:
        problems.append("DEBUG must be false in production")
    if not Config.API_TOKEN and Config.ALLOW_UNAUTHENTICATED:
        problems.append("set FINDRIVO_API_TOKEN or explicitly disable ALLOW_UNAUTHENTICATED")
    if problems:
        raise RuntimeError("Unsafe production configuration: " + "; ".join(problems))


validate_runtime_config()


@app.after_request
def _apply_security_headers(response):
    """Apply low-risk, defense-in-depth headers to every response."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'; "
        "form-action 'self'",
    )
    if request.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


UI_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>FindRivo — Autonomous B2B Lead Engine</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen">
  <div class="max-w-6xl mx-auto py-10 px-4">
    <!-- Header -->
    <div class="flex items-center justify-between pb-8 border-b border-slate-800">
      <div>
        <h1 class="text-3xl font-black tracking-tight text-white flex items-center gap-2">
          FindRivo <span class="text-xs bg-indigo-500/20 text-indigo-400 border border-indigo-500/30 px-2 py-0.5 rounded-full font-mono">Agentic SaaS</span>
        </h1>
        <p class="text-slate-400 text-sm mt-1">Autonomous Maps Discovery, Redesign Qualification & AI Cold Pitch Synthesis</p>
      </div>
      <div id="statusBadge" class="text-xs font-mono bg-slate-900 border border-slate-800 px-3 py-1.5 rounded-lg text-slate-400">
        System Ready
      </div>
    </div>

    <!-- Stats Ribbon -->
    <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mt-6">
      <div class="bg-slate-900/40 border border-slate-800/80 rounded-lg p-3">
        <div class="text-[11px] font-mono text-slate-400">QUALIFIED LEADS</div>
        <div id="statQualified" class="text-2xl font-bold text-indigo-400 mt-1">0</div>
      </div>
      <div class="bg-slate-900/40 border border-slate-800/80 rounded-lg p-3">
        <div class="text-[11px] font-mono text-slate-400">EMAILS HARVESTED</div>
        <div id="statEmails" class="text-2xl font-bold text-emerald-400 mt-1">0</div>
      </div>
      <div class="bg-slate-900/40 border border-slate-800/80 rounded-lg p-3">
        <div class="text-[11px] font-mono text-slate-400">BROKEN / NO SSL</div>
        <div id="statBroken" class="text-2xl font-bold text-rose-400 mt-1">0</div>
      </div>
      <div class="bg-slate-900/40 border border-slate-800/80 rounded-lg p-3">
        <div class="text-[11px] font-mono text-slate-400">LLM CIRCUIT BREAKER</div>
        <div id="statCircuit" class="text-2xl font-bold text-emerald-400 mt-1">CLOSED</div>
      </div>
    </div>

    <!-- Search Form -->
    <div class="mt-6 bg-slate-900/60 border border-slate-800 rounded-xl p-6 shadow-xl backdrop-blur">
      <form id="leadForm" class="space-y-4">
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label class="block text-xs font-medium text-slate-300 uppercase tracking-wider mb-1">Your Agency / Studio Pitch Hook</label>
            <input type="text" id="agencyInput" class="w-full bg-slate-950 border border-slate-800 rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:border-indigo-500"
                   value="Boutique Web Studio specializing in high-speed, modern mobile-first websites with online booking">
          </div>
          <div>
            <label class="block text-xs font-medium text-slate-300 uppercase tracking-wider mb-1">Target Niche in Location</label>
            <input type="text" id="queryInput" class="w-full bg-slate-950 border border-slate-800 rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:border-indigo-500"
                   placeholder="e.g. Dental Clinics in Austin, TX" value="Dental Clinics in Austin, TX" required>
          </div>
        </div>

        <div>
          <label class="block text-xs font-medium text-slate-300 uppercase tracking-wider mb-1">API Token (required when the server enables authentication)</label>
          <input type="password" id="tokenInput" autocomplete="off" class="w-full bg-slate-950 border border-slate-800 rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:border-indigo-500"
                 placeholder="FINDRIVO_API_TOKEN">
        </div>

        <div class="flex flex-wrap items-center justify-between pt-2 gap-4">
          <div class="flex items-center gap-6">
            <div class="flex items-center gap-2">
              <label class="text-xs text-slate-400">Target Leads:</label>
              <input type="number" id="countInput" min="1" max="5000" value="5" class="w-16 bg-slate-950 border border-slate-800 rounded px-2 py-1 text-sm text-center">
            </div>
            <label class="flex items-center gap-2 cursor-pointer text-xs text-slate-400">
              <input type="checkbox" id="onlyRedesignCheck" checked class="rounded bg-slate-950 border-slate-800 text-indigo-600 focus:ring-0">
              <span>Only High-Viability Redesigns</span>
            </label>
          </div>

          <button type="submit" id="submitBtn" class="bg-indigo-600 hover:bg-indigo-500 text-white font-medium text-sm px-6 py-2.5 rounded-lg transition-all shadow-lg shadow-indigo-600/20">
            Start Autonomous Campaign
          </button>
          <button type="button" id="stopBtn" class="hidden bg-rose-600/20 hover:bg-rose-600/30 text-rose-300 border border-rose-500/30 font-medium text-sm px-4 py-2.5 rounded-lg">
            Stop Job
          </button>
        </div>
      </form>
    </div>

    <!-- Restore banner / status note -->
    <div id="restoreBanner" class="mt-6 hidden bg-slate-900/60 border border-slate-800 rounded-xl p-4 text-sm">
      <div class="flex items-start justify-between gap-4">
        <div>
          <div id="restoreTitle" class="font-medium text-slate-200">Restored saved results</div>
          <div id="restoreDetail" class="text-xs text-slate-400 mt-1"></div>
        </div>
        <a id="restoredDownloadBtn" href="#" class="hidden text-xs bg-emerald-600/20 text-emerald-400 border border-emerald-500/30 px-3 py-1.5 rounded-lg font-mono hover:bg-emerald-600/30">
          Download CSV
        </a>
      </div>
    </div>

    <!-- Live discovery progress strip -->
    <div id="liveStatus" class="mt-4 hidden bg-slate-900/60 border border-slate-700/60 rounded-xl p-3 text-xs font-mono text-indigo-300">
      <span id="liveStatusText"></span>
    </div>

    <!-- Live Discovered Leads Table -->
    <div class="mt-6 bg-slate-900/60 border border-slate-800 rounded-xl overflow-hidden shadow-xl">
      <div class="px-6 py-4 border-b border-slate-800 flex items-center justify-between">
        <h2 class="text-sm font-semibold uppercase tracking-wider text-slate-300">Discovered & Audited Leads</h2>
        <span id="tableHint" class="text-[11px] text-slate-500 font-mono">click a row for full details</span>
        <a id="downloadBtn" href="#" class="hidden text-xs bg-emerald-600/20 text-emerald-400 border border-emerald-500/30 px-3 py-1.5 rounded-lg font-mono hover:bg-emerald-600/30">
          Download CSV
        </a>
      </div>
      <div class="overflow-x-auto">
        <table class="w-full text-left text-xs">
          <thead class="bg-slate-950/50 text-slate-400 uppercase font-mono border-b border-slate-800">
            <tr>
              <th class="py-3 px-4">Business</th>
              <th class="py-3 px-4">Contact</th>
              <th class="py-3 px-4">Website</th>
              <th class="py-3 px-4">Evidence & Opportunity</th>
              <th class="py-3 px-4">Pitch Hook</th>
            </tr>
          </thead>
          <tbody id="leadsTableBody" class="divide-y divide-slate-800/60 text-slate-300">
            <tr id="tablePlaceholder">
              <td colspan="5" class="py-8 text-center text-slate-500 font-mono">No saved results. Previous results are restored automatically for 24 hours.</td>
            </tr>
          </tbody>
        </table>
      </div>
     </div>

    <!-- Maintenance / clean start -->
    <details id="maintenance" class="mt-8 bg-slate-900/40 border border-slate-800/80 rounded-xl p-4 text-sm">
      <summary class="cursor-pointer select-none text-slate-300 font-medium">Maintenance — clean start &amp; data deletion</summary>
      <p class="text-xs text-slate-500 mt-2">
        These actions are permanent and cannot be undone. Saved lead/job data lives for a rolling
        24&nbsp;hours by default; logs are truncated (the current files stay in place, active jobs' writing
        is refused). Exports you already downloaded are never touched.
      </p>
      <div class="mt-3 flex flex-wrap gap-3">
        <button type="button" id="clearDataBtn" class="text-xs bg-amber-500/10 text-amber-300 border border-amber-500/30 px-3 py-1.5 rounded-lg hover:bg-amber-500/20">
          Clear saved lead/job data
        </button>
        <button type="button" id="clearLogsBtn" class="text-xs bg-amber-500/10 text-amber-300 border border-amber-500/30 px-3 py-1.5 rounded-lg hover:bg-amber-500/20">
          Clear logs
        </button>
        <button type="button" id="clearAllBtn" class="text-xs bg-rose-500/10 text-rose-300 border border-rose-500/30 px-3 py-1.5 rounded-lg hover:bg-rose-500/20">
          Clear everything
        </button>
      </div>
      <div id="clearResult" class="hidden mt-3 text-xs font-mono border rounded-lg p-3"></div>
    </details>
  </div>

  <script>
    const form = document.getElementById('leadForm');
    const tableBody = document.getElementById('leadsTableBody');
    const statusBadge = document.getElementById('statusBadge');
    const submitBtn = document.getElementById('submitBtn');
    const stopBtn = document.getElementById('stopBtn');
    const downloadBtn = document.getElementById('downloadBtn');
    const restoreBanner = document.getElementById('restoreBanner');
    const liveStatus = document.getElementById('liveStatus');
    const liveStatusText = document.getElementById('liveStatusText');

    let currentEventSource = null;

    const seenKeys = new Set();
    let qualifiedCount = 0;
    let emailCount = 0;
    let brokenCount = 0;

    const STATUS_STYLES = {
      created:  'bg-slate-900 border-slate-800 text-slate-400',
      running:  'bg-amber-500/20 text-amber-400 border-amber-500/30',
      interrupted: 'bg-orange-500/20 text-orange-300 border-orange-500/30',
      complete: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
      failed:   'bg-rose-500/20 text-rose-400 border-rose-500/30'
    };
    const STATUS_LABELS = {
      created: 'System Ready',
      running: 'Saved job still running (stream not resumable; start a new campaign to refresh)',
      interrupted: 'Previous job was interrupted — partial results restored',
      complete: 'Restored completed results',
      failed: 'Previous job failed — partial results restored'
    };

    function setBadge(status) {
      statusBadge.innerText = STATUS_LABELS[status] || status;
      statusBadge.className = 'text-xs font-mono px-3 py-1.5 rounded-lg border ' + (STATUS_STYLES[status] || STATUS_STYLES.created);
    }

    function fmt(value, missingText) {
      if (value === null || value === undefined || String(value).trim() === '' || value === 'None') {
        const span = document.createElement('span');
        span.className = 'text-slate-600';
        span.textContent = missingText || 'not found';
        return span;
      }
      return document.createTextNode(String(value));
    }

    function leadKey(lead) {
      if (lead.maps_link) return 'maps:' + String(lead.maps_link).split('?')[0];
      return 'fallback:' + [lead.name, lead.address, lead.phone].join('|');
    }

    function chip(text, cls) {
      const span = document.createElement('span');
      span.className = 'px-1.5 py-0.5 rounded text-[10px] border ' + cls;
      span.textContent = text;
      return span;
    }

    function responseTimeText(lead) {
      const rt = lead.response_time || {};
      if (!lead.load_time_seconds && !rt.seconds) {
        const span = document.createElement('span');
        span.className = 'text-slate-600';
        span.textContent = 'no measurement';
        return span;
      }
      const seconds = rt.seconds != null ? rt.seconds : lead.load_time_seconds;
      const basis = rt.basis === 'median_of_2_http_get_durations' ? 'median of 2 HTTP GETs'
                  : 'single HTTP GET (noisy)';
      const div = document.createElement('div');
      div.textContent = `${seconds}s — ${basis}`;
      return div;
    }

    function evidenceChips(lead) {
      const cell = document.createElement('td');
      cell.className = 'py-3 px-4';
      const level = lead.confidence_level || (lead.extraction_confidence != null ? 'low' : 'unknown');
      const levelCls = level === 'high' ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30'
                    : level === 'medium' ? 'bg-amber-500/20 text-amber-300 border-amber-500/30'
                    : 'bg-slate-700/40 text-slate-300 border-slate-600/40';
      const chipsWrap = document.createElement('div');
      chipsWrap.className = 'flex flex-wrap gap-1 mb-1';
      chipsWrap.append(
        chip(`evidence: ${level}`, levelCls),
        chip(`opportunity: ${lead.opportunity_score != null ? lead.opportunity_score : '—'}`,
             'bg-indigo-500/20 text-indigo-300 border-indigo-500/30')
      );
      if (lead.outreach_suitability) {
        const suitCls = lead.outreach_suitability === 'ready'
          ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30'
          : 'bg-slate-700/40 text-slate-400 border-slate-600/40';
        chipsWrap.append(chip(`outreach: ${lead.outreach_suitability}`, suitCls));
      }
      if ((lead.field_conflicts && Object.keys(lead.field_conflicts).length)) {
        chipsWrap.append(chip('conflicting data', 'bg-rose-500/20 text-rose-300 border-rose-500/30'));
      }
      cell.append(chipsWrap);
      return cell;
    }

    function detailRow(lead) {
      const tr = document.createElement('tr');
      tr.className = 'hidden bg-slate-950/70';
      const td = document.createElement('td');
      td.colSpan = 5;
      td.className = 'py-4 px-6';

      const grid = document.createElement('div');
      grid.className = 'grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-2 text-[11px]';

      const addRow = (title, node) => {
        const wrap = document.createElement('div');
        wrap.className = 'flex gap-2 border-b border-slate-800/60 py-1';
        const dt = document.createElement('div');
        dt.className = 'text-slate-500 w-36 shrink-0 font-mono';
        dt.textContent = title;
        const dd = document.createElement('div');
        dd.className = 'text-slate-300 break-words';
        if (typeof node === 'string') {
          dd.textContent = node || '—';
          if (!node) dd.className += ' text-slate-600';
        } else {
          dd.append(node);
        }
        wrap.append(dt, dd);
        grid.append(wrap);
      };

      addRow('Rating', `${lead.rating || '—'} (${lead.reviews || 0} reviews)`);
      addRow('Address', lead.address || '');
      addRow('All emails', (lead.all_emails || []).join('; '));
      addRow('All phones', (lead.all_phones || []).join('; '));
      addRow('Contact page', lead.contact_form || '');
      addRow('SSL', `${lead.has_ssl ? 'Yes' : 'No / unknown'}${lead.ssl_days_remaining != null ? ` (expires in ${lead.ssl_days_remaining} days)` : ''}`);
      addRow('Defects', (lead.audit_defects || []).join('; '));
      addRow('Company info', lead.company_description || lead.one_liner || '');
      addRow('Angle', lead.suggested_outreach_angle || '');
      addRow('Evidence by field',
        Object.entries(lead.evidence || {}).map(([f, s]) => `${f}: ${s}`).join(' | ') || '—');
      addRow('Sources', lead.data_sources || '');
      addRow('Opportunity reasons', (lead.opportunity_reasons || []).join(' | '));
      addRow('Critic verdict', lead.design_verdict ? `${lead.design_verdict} (viability ${lead.viability_score ?? '—'})` : '');
      addRow('Outreach note', lead.outreach_note || '');

      td.append(grid);
      tr.append(td);
      tr.dataset.detail = '1';
      return tr;
    }

    function renderLead(lead) {
      const key = leadKey(lead);
      if (seenKeys.has(key)) return false;
      seenKeys.add(key);

      const placeholder = document.getElementById('tablePlaceholder');
      if (placeholder) placeholder.remove();

      // Row built with textContent so scraped data can never execute in the browser.
      const row = document.createElement('tr');
      row.className = 'hover:bg-slate-800/40 cursor-pointer';

      const businessCell = document.createElement('td');
      businessCell.className = 'py-3 px-4 font-medium text-white';
      const businessName = document.createElement('div');
      businessName.textContent = lead.name || 'Unknown Business';
      const website = document.createElement('div');
      website.className = 'text-[10px] text-slate-500 break-all';
      website.append(fmt(lead.website, 'no website listed'));
      businessCell.append(businessName, website);

      const contactCell = document.createElement('td');
      contactCell.className = 'py-3 px-4';
      const phone = document.createElement('div');
      phone.append(fmt(lead.phone));
      const email = document.createElement('div');
      email.className = 'text-[10px] text-indigo-400 break-all';
      email.append(fmt(lead.email));
      contactCell.append(phone, email);

      const websiteCell = document.createElement('td');
      websiteCell.className = 'py-3 px-4 font-mono';
      const verdict = document.createElement('div');
      verdict.className = 'px-1.5 py-0.5 rounded text-[10px] inline-block ' + (lead.needs_redesign
        ? 'bg-rose-500/20 text-rose-300 border border-rose-500/30'
        : 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30');
      verdict.textContent = lead.website_status
        ? `${lead.website_status}`
        : (lead.needs_redesign ? 'needs redesign' : 'functional');
      const rtWrapper = document.createElement('div');
      rtWrapper.className = 'text-[10px] text-slate-500 mt-1';
      rtWrapper.append(responseTimeText(lead));
      websiteCell.append(verdict, rtWrapper);

      const scoreCell = evidenceChips(lead);

      const pitchCell = document.createElement('td');
      pitchCell.className = 'py-3 px-4 text-slate-300 italic max-w-[18rem] whitespace-normal';
      pitchCell.append(fmt(lead.ai_cold_pitch_hook));

      row.append(businessCell, contactCell, websiteCell, scoreCell, pitchCell);

      const detail = detailRow(lead);
      row.addEventListener('click', () => detail.classList.toggle('hidden'));

      tableBody.append(row, detail);

      // Stats (idempotent per lead because keys dedupe re-entry).
      qualifiedCount++;
      if (lead.email) emailCount++;
      if (!lead.is_live || !lead.has_ssl) brokenCount++;
      document.getElementById('statQualified').innerText = qualifiedCount;
      document.getElementById('statEmails').innerText = emailCount;
      document.getElementById('statBroken').innerText = brokenCount;
      return true;
    }

    function showDownload(filename) {
      downloadBtn.href = `/api/download/${encodeURIComponent(filename)}`;
      downloadBtn.classList.remove('hidden');
    }

    async function restorePreviousResults() {
      try {
        const res = await fetch('/api/results/latest');
        if (!res.ok) return;
        const state = await res.json();
        if (!state.restored) return;
        const leads = state.leads || [];
        if (!leads.length) return;

        let restored = 0;
        for (const lead of leads) {
          if (renderLead(lead)) restored++;
        }
        if (!restored) return;

        setBadge(state.status || 'created');
        restoreBanner.classList.remove('hidden');
        document.getElementById('restoreTitle').textContent = STATUS_LABELS[state.status] || 'Restored saved results';
        const when = state.updated_at ? new Date(state.updated_at * 1000).toLocaleString() : 'unknown time';
        document.getElementById('restoreDetail').textContent =
          `${restored} lead(s) restored from a previous job (saved ${when}). ` +
          `Results are kept ${state.retention_hours} hours; duplicates are skipped automatically.`;
        if (state.csv_filename) showDownload(state.csv_filename);
      } catch (err) {
        // Restore errors must never block a fresh campaign.
        console && console.warn && console.warn('Restore failed:', err);
      }
    }

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      tableBody.innerHTML = '';
      seenKeys.clear();
      qualifiedCount = 0;
      emailCount = 0;
      brokenCount = 0;
      document.getElementById('statQualified').innerText = 0;
      document.getElementById('statEmails').innerText = 0;
      document.getElementById('statBroken').innerText = 0;
      restoreBanner.classList.add('hidden');

      submitBtn.disabled = true;
      submitBtn.innerText = 'Pipeline Running...';
      stopBtn.classList.remove('hidden');
      statusBadge.innerText = '● Streaming discovery & audits';
      statusBadge.className = 'text-xs font-mono bg-amber-500/20 text-amber-400 border border-amber-500/30 px-3 py-1.5 rounded-lg';
      liveStatus.classList.remove('hidden');
      liveStatusText.textContent = 'Starting pipeline…';

      const payload = {
        query: document.getElementById('queryInput').value,
        agency_services: document.getElementById('agencyInput').value,
        target_count: parseInt(document.getElementById('countInput').value),
        only_redesign: document.getElementById('onlyRedesignCheck').checked
      };

      const apiToken = document.getElementById('tokenInput').value.trim();
      const headers = { 'Content-Type': 'application/json' };
      if (apiToken) headers['X-API-Key'] = apiToken;

      const res = await fetch('/api/pipeline/start', {
        method: 'POST',
        headers: headers,
        body: JSON.stringify(payload)
      });
      if (!res.ok) {
        const errorBody = await res.json().catch(() => ({}));
        statusBadge.innerText = errorBody.error || `Request failed (${res.status})`;
        statusBadge.className = 'text-xs font-mono bg-rose-500/20 text-rose-400 border border-rose-500/30 px-3 py-1.5 rounded-lg';
        submitBtn.disabled = false;
        submitBtn.innerText = 'Start Autonomous Campaign';
        return;
      }
      const data = await res.json();
      const taskId = data.task_id;
      const tokenQuery = data.token ? `?token=${encodeURIComponent(data.token)}` : '';

      const eventSource = new EventSource(`/api/pipeline/stream/${encodeURIComponent(taskId)}${tokenQuery}`);
      currentEventSource = eventSource;

      eventSource.onmessage = (event) => {
        const lead = JSON.parse(event.data);

        if (lead.__progress__) {
          liveStatusText.textContent = lead.message || 'Working…';
          return;
        }

        if (lead.__event__ === 'done') {
          eventSource.close();
          currentEventSource = null;
          finishJobUI('complete');
          if (lead.filename) showDownload(lead.filename);
          return;
        }

        liveStatusText.textContent = 'Lead processed: ' + (lead.name || 'unknown business');
        renderLead(lead);
      };
      eventSource.onerror = () => {
        eventSource.close();
        currentEventSource = null;
        finishJobUI('interrupted');
      };
    });

    function finishJobUI(status) {
      submitBtn.disabled = false;
      submitBtn.innerText = 'Start Autonomous Campaign';
      stopBtn.classList.add('hidden');
      setBadge(status);
      // Fetch the authoritative snapshot so the table reflects everything the
      // server saved, including leads that arrived after the stream ended.
      fetch('/api/results/latest').then(r => r.ok ? r.json() : null).then(state => {
        if (!state || !state.restored) return;
        const fresh = (state.leads || []).filter(l => !seenKeys.has(leadKey(l)));
        let added = 0;
        for (const lead of fresh) { if (renderLead(lead)) added++; }
        if (added) {
          document.getElementById('restoreTitle').textContent = 'Synced final results from the server';
          document.getElementById('restoreDetail').textContent = `${added} lead(s) added after the stream closed.`;
          restoreBanner.classList.remove('hidden');
        }
        if (state.csv_filename) showDownload(state.csv_filename);
      }).catch(() => {});
    }

    stopBtn.addEventListener('click', () => {
      stopBtn.disabled = true;
      liveStatusText.textContent = 'Stopping — waiting for in-flight work to finish…';
      if (currentEventSource) {
        // Closing the SSE stream is the stop signal: the server marks the job
        // interrupted and cancels remaining discovery work.
        currentEventSource.close();
        currentEventSource = null;
      }
      finishJobUI('interrupted');
      stopBtn.disabled = false;
    });

    // ---------------- Maintenance: clear data / logs / all ----------------
    const clearButtons = [
      { id: 'clearDataBtn', mode: 'data', label: 'Clear saved lead/job data' },
      { id: 'clearLogsBtn', mode: 'logs', label: 'Clear logs' },
      { id: 'clearAllBtn', mode: 'all', label: 'Clear everything' },
    ];
    for (const { id, mode, label } of clearButtons) {
      document.getElementById(id).addEventListener('click', async () => {
        const what = mode === 'all'
          ? 'saved lead/job data AND logs'
          : mode === 'logs' ? 'application log files' : 'saved lead/job data (results and progress)';
        if (!confirm(`Delete ${what}? This cannot be undone.`)) return;
        const box = document.getElementById('clearResult');
        box.classList.remove('hidden', 'text-emerald-300', 'text-rose-300');
        box.classList.add('text-slate-400');
        box.textContent = 'Clearing…';
        const apiToken = document.getElementById('tokenInput').value.trim();
        const headers = { 'Content-Type': 'application/json' };
        if (apiToken) headers['X-API-Key'] = apiToken;
        try {
          const res = await fetch('/api/maintenance/clear', {
            method: 'POST',
            headers,
            body: JSON.stringify({ mode, confirm: true })
          });
          const body = await res.json().catch(() => ({}));
          if (!res.ok || body.success === false) {
            box.classList.remove('text-slate-400');
            box.classList.add('text-rose-300');
            box.textContent = 'Clear failed: ' + (body.error || `HTTP ${res.status}`);
            return;
          }
          box.classList.remove('text-slate-400');
          box.classList.add('text-emerald-300');
          const nData = (body.cleared_data && body.cleared_data.deleted || []).length;
          const nLogs = (body.cleared_logs && body.cleared_logs.cleared || []).length;
          box.textContent = `Cleared ${nData} data file(s) and ${nLogs} log file(s). Run the command again to confirm state is clean.`;
          // Reset the local table so old results cannot linger or reappear.
          tableBody.innerHTML = '';
          seenKeys.clear();
          qualifiedCount = 0; emailCount = 0; brokenCount = 0;
          document.getElementById('statQualified').innerText = 0;
          document.getElementById('statEmails').innerText = 0;
          document.getElementById('statBroken').innerText = 0;
          restoreBanner.classList.add('hidden');
          liveStatus.classList.add('hidden');
          downloadBtn.classList.add('hidden');
          setBadge('created');
        } catch (err) {
          box.classList.remove('text-slate-400');
          box.classList.add('text-rose-300');
          box.textContent = 'Clear failed: network error';
        }
      });
    }

    restorePreviousResults();
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(UI_TEMPLATE)


@app.route("/api/pipeline/start", methods=["POST"])
@require_api_auth
@rate_limited
def start_pipeline():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    query = data.get("query", "Dental Clinic in Austin, TX")
    agency_services = data.get("agency_services")
    only_redesign_raw = data.get("only_redesign", True)

    if not isinstance(query, str):
        return jsonify({"error": "query must be a string"}), 400
    query = query.strip()
    if not query or len(query) > 500:
        return jsonify({"error": "query must be between 1 and 500 characters"}), 400

    if agency_services is not None:
        if not isinstance(agency_services, str):
            return jsonify({"error": "agency_services must be a string"}), 400
        agency_services = agency_services.strip()
        if len(agency_services) > 1000:
            return jsonify({"error": "agency_services must be at most 1000 characters"}), 400
        if not agency_services:
            agency_services = None

    try:
        target_count = _parse_target_count(data.get("target_count", 5))
    except ValueError as err:
        return jsonify({"error": str(err)}), 400

    if isinstance(only_redesign_raw, bool):
        only_redesign = only_redesign_raw
    else:
        only_redesign = str(only_redesign_raw).strip().lower() in ("true", "1", "yes")

    if shutdown_core.is_shutting_down():
        return jsonify({"error": "Server is shutting down; new jobs are not accepted"}), 503

    if not _JOBS_SEMAPHORE.acquire(blocking=False):
        return jsonify({"error": "Too many active pipeline jobs; retry later"}), 429

    try:
        _cleanup_streams()
        task_id = str(uuid.uuid4())
        stream_queue = queue.Queue()
        csv_filename = Path(build_export_filename(query)).name
        capability_token = secrets.token_urlsafe(32)
        cancel_event = threading.Event()
        recorder = _JobRecorder(task_id, query, agency_services, target_count, csv_filename)

        with _ACTIVE_STREAMS_LOCK:
            ACTIVE_STREAMS[task_id] = {
                "queue": stream_queue,
                "created": time.time(),
                "csv_filename": csv_filename,
                "token": capability_token,
                "cancel_event": cancel_event,
                "recorder": recorder,
            }

        def background_runner():
            try:
                for lead in pipeline.execute(
                    user_query=query,
                    agency_services=agency_services,
                    target_count=target_count,
                    only_qualified_redesigns=only_redesign,
                    csv_filename=csv_filename,
                    cancel_event=cancel_event,
                    # Discovery progress (feed scraped, place X/Y) is streamed
                    # as lightweight events; leads follow as usual.
                    progress_callback=lambda event: stream_queue.put(event),
                ):
                    stream_queue.put(lead)
                    recorder.add_lead(lead)
            except Exception as err:
                app_logger.error("Pipeline runner error: %s", err)
                recorder.mark(persistence.STATUS_FAILED)
            else:
                recorder.mark(
                    persistence.STATUS_INTERRUPTED
                    if cancel_event.is_set()
                    else persistence.STATUS_COMPLETE
                )
            finally:
                stream_queue.put({"__event__": "done", "filename": csv_filename})
                _JOBS_SEMAPHORE.release()

        try:
            thread = threading.Thread(target=background_runner, daemon=True)
            thread.start()
        except Exception:
            with _ACTIVE_STREAMS_LOCK:
                ACTIVE_STREAMS.pop(task_id, None)
            recorder.mark(persistence.STATUS_FAILED)
            _JOBS_SEMAPHORE.release()
            raise

        return jsonify({
            "task_id": task_id,
            "csv_filename": csv_filename,
            "token": capability_token,
        })
    except Exception:
        _JOBS_SEMAPHORE.release()
        raise


@app.route("/api/pipeline/stream/<task_id>")
def stream_results(task_id: str):
    with _ACTIVE_STREAMS_LOCK:
        entry = ACTIVE_STREAMS.get(task_id)
    if not entry:
        return Response("Invalid or expired stream task ID", status=404)

    if not (_master_auth_valid() or _capability_valid(entry)):
        return _auth_failure_response()

    stream_queue = entry["queue"]

    def event_stream():
        try:
            while True:
                try:
                    lead = stream_queue.get(timeout=25.0)
                    yield f"data: {json.dumps(lead)}\n\n"
                    if lead.get("__event__") == "done":
                        break
                except queue.Empty:
                    yield ": ping\n\n"
        finally:
            # Client disconnected or stream ended: stop expensive remaining work
            # and make sure the persisted snapshot reflects the interruption.
            entry.get("cancel_event", threading.Event()).set()
            recorder = entry.get("recorder")
            if recorder is not None and recorder.snapshot().get("status") == persistence.STATUS_RUNNING:
                recorder.mark(persistence.STATUS_INTERRUPTED)

    return Response(event_stream(), mimetype="text/event-stream")


@app.route("/api/download/<filename>")
def download_csv(filename: str):
    file_path = _safe_export_path(filename)
    if not file_path or not file_path.exists():
        return jsonify({"error": "Export file not found"}), 404

    if not _master_auth_valid():
        with _ACTIVE_STREAMS_LOCK:
            entries = list(ACTIVE_STREAMS.values())
        if not any(
            entry.get("csv_filename") == filename and _capability_valid(entry)
            for entry in entries
        ):
            return _auth_failure_response()

    return send_file(file_path, as_attachment=True, download_name=file_path.name)


@app.route("/api/results/latest")
def latest_results():
    """Most recent job snapshot so a page reload restores prior results."""
    _cleanup_streams()
    state = persistence.latest_job()
    if not state:
        return jsonify({"restored": False, "retention_hours": Config.RESULTS_RETENTION_HOURS})
    leads = persistence.deduplicate_leads(state.get("leads") or [])
    return jsonify({
        "restored": True,
        "retention_hours": Config.RESULTS_RETENTION_HOURS,
        "task_id": state.get("task_id"),
        "status": state.get("status"),
        "query": state.get("query"),
        "agency_services": state.get("agency_services"),
        "target_count": state.get("target_count"),
        "csv_filename": state.get("csv_filename"),
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
        "leads": leads,
    })


@app.route("/api/results/<task_id>")
def task_results(task_id: str):
    state = persistence.load_job(str(task_id))
    if not state:
        return jsonify({"error": "Result snapshot not found or expired"}), 404
    leads = persistence.deduplicate_leads(state.get("leads") or [])
    payload = dict(state)
    payload["leads"] = leads
    payload["retention_hours"] = Config.RESULTS_RETENTION_HOURS
    return jsonify(payload)


@app.route("/api/maintenance/clear", methods=["POST"])
@require_api_auth
@rate_limited
def clear_app_data():
    """Clear-start maintenance: delete saved lead data, logs, or both.

    Body: {"mode": "data"|"logs"|"all", "confirm": true}
    Refuses destructively operating while a pipeline job is still running
    because that job is actively writing its snapshot and logs.
    """
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    mode = str(data.get("mode") or "")
    if mode not in ("data", "logs", "all"):
        return jsonify({"error": "mode must be one of: data, logs, all"}), 400
    if data.get("confirm") is not True:
        return jsonify({"error": "Destructive action requires confirm=true in the request body"}), 400

    with _ACTIVE_STREAMS_LOCK:
        running_jobs = [
            entry["recorder"].snapshot()
            for entry in ACTIVE_STREAMS.values()
            if entry.get("recorder") is not None
        ]
    if any(state.get("status") == persistence.STATUS_RUNNING for state in running_jobs):
        return jsonify({
            "error": "A pipeline job is still running and is writing data. "
            "Stop the job (or wait for it to finish) before clearing stored data."
        }), 409

    response: dict = {"mode": mode, "cleared_data": None, "cleared_logs": None, "success": True}
    try:
        if mode in ("data", "all"):
            response["cleared_data"] = maintenance.clear_saved_data()
            if response["cleared_data"].get("errors"):
                response["success"] = False
        if mode in ("logs", "all"):
            response["cleared_logs"] = maintenance.clear_logs()
            if response["cleared_logs"].get("errors"):
                response["success"] = False
    except Exception as err:
        app_logger.error("Maintenance clear failed unexpectedly: %s", err)
        response["success"] = False
        response["error"] = "Clear operation failed; check server logs."
    # Save errors are visible but not fatal to the request; report them verbatim.
    if response["cleared_data"] and response["cleared_data"].get("errors"):
        response["error"] = "Some saved-data files could not be removed (see cleared_data.errors)."
    if response["cleared_logs"] and response["cleared_logs"].get("errors"):
        response.setdefault("error", "Some log files could not be truncated (see cleared_logs.errors).")
    return jsonify(response)


@app.route("/api/health")
def health():
    _cleanup_streams()
    return jsonify({
        "status": "healthy",
        "gemini_circuit": pipeline.llm.gemini_circuit._state,
        "groq_keys_available": len(Config.get_groq_keys()),
        "authentication_required": bool(Config.API_TOKEN) or not Config.ALLOW_UNAUTHENTICATED,
        "storage": {
            "local_snapshots": "enabled",
            "retention_hours": Config.RESULTS_RETENTION_HOURS,
        },
    })


# ==========================================
# CLI HARNESS & DIAGNOSTICS
# ==========================================
def run_diagnostics():
    """Validates local environment, LLM keys, and browser binaries."""
    print("\n🔍 FINDRIVO SYSTEM DIAGNOSTICS")
    print("-" * 50)

    # 1. Gemini
    gemini_key = Config.GEMINI_API_KEY
    if gemini_key:
        print("✔ GEMINI_API_KEY: Configured")
        try:
            res = pipeline.llm.call_gemini("Reply 'OK'", temperature=0.0)
            print(f"  └─ Gemini API Ping: SUCCESS ('{res.strip()[:20]}')")
        except Exception as e:
            print(f"  └─ Gemini API Ping: FAILED ({e})")
    else:
        print("⚠ GEMINI_API_KEY: Not configured in .env")

    # 2. Groq
    groq_keys = Config.get_groq_keys()
    print(f"✔ GROQ_API_KEYS: {len(groq_keys)} configured")
    if groq_keys:
        try:
            res = pipeline.llm.call_groq("Reply 'OK'", temperature=0.0)
            print(f"  └─ Groq API Ping: SUCCESS ('{res.strip()[:20]}')")
        except Exception as e:
            print(f"  └─ Groq API Ping: FAILED ({e})")

    # 3. Playwright
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        print("✔ Playwright Chromium: Installed and launchable")
    except Exception as e:
        print(f"❌ Playwright Chromium: FAILED ({e})")
        print("   -> Fix: Run 'pip install playwright && playwright install chromium'")

    print("-" * 50 + "\n")


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="FindRivo Autonomous B2B Lead Gen SaaS & CLI")
    parser.add_argument("--cli", action="store_true", help="Execute directly via CLI")
    parser.add_argument("--doctor", action="store_true", help="Run system diagnostics on APIs and browser")
    parser.add_argument("--query", type=str, default="Dental Clinic in Austin, TX", help="Search query (Niche in City)")
    parser.add_argument("--agency", type=str, default="High-speed mobile website redesign and online booking systems", help="Agency service description")
    parser.add_argument("--count", type=int, default=5, help="Target lead count")
    parser.add_argument("--all", action="store_true", help="Include non-defective websites")
    parser.add_argument("--port", type=int, default=Config.PORT, help="Web server port")

    args = parser.parse_args(argv)

    if args.doctor:
        run_diagnostics()
        return 0

    if args.cli:
        manager = shutdown_core.install_handlers("cli")
        print("=" * 75)
        print("🚀 FINDRIVO AGENTIC PIPELINE (CLI MODE)")
        print(f"Query:  {args.query}")
        print(f"Agency: {args.agency}")
        print(f"Target: {args.count} leads")
        print("=" * 75)

        collected: list = []
        csv_file: str = ""
        interrupted = False
        try:
            csv_file, collected = pipeline.run_to_csv(
                user_query=args.query,
                agency_services=args.agency,
                target_count=args.count,
                only_qualified_redesigns=not args.all,
            )
        except KeyboardInterrupt:
            interrupted = True
            print("\n" + "-" * 75)
            print("Interrupt received mid-run; results collected so far are preserved.")
        else:
            print("\n" + "=" * 75)
            print(f"✅ COMPLETED! Extracted {len(collected)} qualified leads.")
            print(f"📁 Qualified CSV: {csv_file}")
            all_csv = Path(csv_file).with_name(Path(csv_file).stem + "_all" + Path(csv_file).suffix)
            if all_csv.exists():
                print(f"📁 All-leads CSV (incl. skipped): {all_csv}")
            print("=" * 75)

            for idx, l in enumerate(collected, start=1):
                print(f"\n[{idx}] {l.get('name')} ({l.get('website') or 'No Website'})")
                print(f"    Phone:   {l.get('phone') or 'not found'} | Email: {l.get('email') or 'not found'}")
                print(f"    Verdict: {l.get('design_verdict')} (Redesign viability: {l.get('viability_score', 'n/a')}/100)")
                print(f"    Defects: {'; '.join(l.get('audit_defects', []))}")
                print(f"    Pitch:   \"{l.get('ai_cold_pitch_hook')}\"")

        if interrupted:
            # Finish the graceful shutdown started by the signal handler.
            manager.run_cleanup()
            print(f"\nCollected {len(collected)} lead(s) before shutdown; already extracted results are preserved.")
            if csv_file and Path(csv_file).exists():
                print(f"📁 Partial CSV: {csv_file}")
            print("=" * 75)
            return manager.exit_code()
        return 0
    else:
        shutdown_core.install_handlers("server")
        print(f"FindRivo SaaS Interface active on http://localhost:{args.port}")
        app.run(port=args.port, debug=Config.DEBUG, use_reloader=False)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())