"""
ReviewLoop
Starten: python app.py
Browser: http://localhost:5000
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
import webbrowser
from base64 import b64decode
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from pipeline.config_alias import dashboard_config_path, load_dashboard_config, resolve_project_path
from pipeline.i18n import SUPPORTED_LANGS, get_language, translate
from pipeline.telemetry import capture as capture_telemetry
from pipeline import state_machine as sm
from pipeline.konzertmeister import (
    consolidate_reviews,
    generate_cursor_task,
    generate_review_yaml,
)
from pipeline.cursor_bridge import write_cursor_task
from pipeline.reviewer import (
    clear_review_state,
    get_review_results,
    get_review_state,
    is_review_complete,
    run_parallel_reviews,
)


# ── App Setup ────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.urandom(24)
APP_LOGGER = logging.getLogger("reviewloop.dashboard")


def _auth_enabled() -> bool:
    return bool(os.environ.get("REVIEWLOOP_BASIC_AUTH_PASSWORD"))


def _is_authorized() -> bool:
    if not _auth_enabled():
        return True
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = b64decode(header.split(" ", 1)[1]).decode("utf-8")
        _, password = decoded.split(":", 1)
    except Exception:
        return False
    expected = os.environ.get("REVIEWLOOP_BASIC_AUTH_PASSWORD", "")
    return secrets.compare_digest(password, expected)


@app.before_request
def _optional_basic_auth():
    if request.path.startswith("/static/") or _is_authorized():
        return None
    return (
        "Authentication required",
        401,
        {"WWW-Authenticate": 'Basic realm="ReviewLoop"'},
    )


@app.after_request
def _set_headers(response):
    """
    Headers that prevent browser extensions from scanning/injecting into the app.
    - X-Content-Type-Options: stops MIME-type sniffing (Wappalyzer trigger)
    - X-Frame-Options: no iframing
    - Permissions-Policy: disables features extensions probe for
    - Cache-Control: prevents stale API responses being analysed by extensions
    """
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Permissions-Policy"] = "interest-cohort=()"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    # For API endpoints: no caching so extensions don't re-scan old responses
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def _cfg_dir(cfg: dict, section: str, default_relative: str) -> Path:
    return resolve_project_path(cfg.get(section, {}).get("path"), default_relative=default_relative)


def _workspace_root_paths(cfg: dict) -> list[Path]:
    return [
        resolve_project_path(r.get("path"), default_relative=".")
        for r in cfg.get("workspace_roots", [])
    ]


def _load_config() -> dict:
    config_path = dashboard_config_path(BASE_DIR)
    return load_dashboard_config(config_path, logger=logging.getLogger("reviewloop.dashboard"))


def _load_env():
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


_load_env()


def _get_api_keys() -> dict:
    """Merge session keys with environment variables. Session takes priority."""
    keys_from_env = {
        "google":    os.environ.get("GOOGLE_API_KEY", ""),
        "anthropic": os.environ.get("ANTHROPIC_API_KEY", ""),
        "openai":    os.environ.get("OPENAI_API_KEY", ""),
        "deepseek":  os.environ.get("DEEPSEEK_API_KEY", ""),
    }
    keys_from_session = session.get("api_keys", {})
    merged = {**keys_from_env, **{k: v for k, v in keys_from_session.items() if v}}
    return merged


@app.context_processor
def _inject_market_ready_context():
    lang = get_language(request.cookies.get("lang"))
    return {
        "lang": lang,
        "supported_langs": SUPPORTED_LANGS,
        "t": lambda key, **kwargs: translate(key, lang, **kwargs),
        "has_any_key": any(_get_api_keys().values()),
    }


def _has_keys_for_phase(phase_idx: int, api_keys: dict) -> tuple[bool, str]:
    """Check if we have the required API keys for a given phase."""
    cfg = _load_config()
    if phase_idx in (0, 2, 3):
        provider = cfg.get("teamleiter", {}).get("provider", "anthropic")
        if provider != "demo" and not api_keys.get(provider):
            return False, f"API-Key fuer Orchestrator ({provider}) fehlt."
    if phase_idx == 1:
        reviewers = cfg.get("reviewer", {})
        missing = []
        for ki_id, rcfg in reviewers.items():
            prov = rcfg.get("provider", "anthropic")
            if prov != "demo" and not api_keys.get(prov):
                missing.append(f"{rcfg.get('name', ki_id)} ({prov})")
        if missing:
            return False, f"API-Keys fehlen fuer: {', '.join(missing)}"
    return True, ""


def _safe_title(value: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in value.strip())
    return (safe or "run")[:48]


def _phase2_markdown(run, phase_result: dict[str, Any]) -> str:
    results = phase_result.get("results", {})
    errors = phase_result.get("errors", {})
    lines = [
        f"# Phase-2 Review-Ergebnisse: {run.title}",
        f"Run-ID: {run.run_id}",
        f"Datum: {run.created_at[:10]}",
        f"Reviewer: {len(results)} erfolgreich, {len(errors)} Fehler",
        "",
    ]
    for ki_id, result in results.items():
        lines += [
            "---",
            f"## {result.get('ki_name', ki_id)}",
            f"**Rolle:** {result.get('role', '—')}  |  **Dauer:** {result.get('duration', '?')}s",
            "",
            result.get("text", ""),
            "",
        ]
    if errors:
        lines += ["---", "## Fehler", ""]
        for ki_id, err in errors.items():
            lines.append(f"- **{err.get('ki_name', ki_id)}:** {err.get('error', '?')}")
    return "\n".join(lines)


def _phase3_markdown(run, phase_result: dict[str, Any]) -> str:
    lines = [
        f"# Phase-3 Konsolidierung: {run.title}",
        f"Run-ID: {run.run_id}",
        f"Datum: {run.created_at[:10]}",
        "",
        phase_result.get("consolidation_text", ""),
        "",
    ]
    return "\n".join(lines)


def _save_markdown(content: str, save_dir: Path, prefix: str, run) -> Path:
    save_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{prefix}_{_safe_title(run.title)}_{run.run_id[:8]}_{ts}.md"
    target = save_dir / filename
    target.write_text(content, encoding="utf-8")
    return target


def _save_phase2_report(run, phase_result: dict[str, Any]) -> Path:
    cfg = _load_config()
    save_dir = _cfg_dir(cfg, "review_results_dir", "data/review_ergebnisse")
    return _save_markdown(_phase2_markdown(run, phase_result), save_dir, "review_ergebnisse", run)


def _save_phase3_report(run, phase_result: dict[str, Any]) -> Path:
    cfg = _load_config()
    save_dir = _cfg_dir(cfg, "konsolidierungen_dir", "data/konsolidierungen")
    return _save_markdown(_phase3_markdown(run, phase_result), save_dir, "konsolidierung", run)


def _as_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _build_token_report_dict(run) -> dict[str, Any]:
    phases_payload: list[dict[str, Any]] = []
    totals = {
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "thinking_tokens": 0,
        "duration_seconds": 0.0,
    }

    for idx, phase in enumerate(run.phases, start=1):
        result = phase.result or {}
        phase_payload: dict[str, Any] = {
            "phase_idx": idx,
            "phase_id": phase.id,
            "phase_label": phase.label,
            "status": phase.status,
            "duration_seconds": round(_as_float(result.get("duration")), 3),
            "cache_read_tokens": _as_int(result.get("cache_read_tokens")),
            "cache_write_tokens": _as_int(result.get("cache_write_tokens")),
            "thinking_tokens": _as_int(result.get("thinking_budget")),
            "km_model": result.get("km_model"),
            "km_provider": result.get("km_provider"),
        }

        totals["duration_seconds"] += phase_payload["duration_seconds"]
        totals["cache_read_tokens"] += phase_payload["cache_read_tokens"]
        totals["cache_write_tokens"] += phase_payload["cache_write_tokens"]
        totals["thinking_tokens"] += phase_payload["thinking_tokens"]

        if idx == 2:
            reviewer_results = result.get("results", {})
            reviewer_items: list[dict[str, Any]] = []
            for reviewer_id, reviewer_result in reviewer_results.items():
                reviewer_payload = {
                    "reviewer_id": reviewer_id,
                    "reviewer_name": reviewer_result.get("ki_name", reviewer_id),
                    "role": reviewer_result.get("role", ""),
                    "duration_seconds": round(_as_float(reviewer_result.get("duration")), 3),
                    "cache_read_tokens": _as_int(reviewer_result.get("cache_read_tokens")),
                    "cache_write_tokens": _as_int(reviewer_result.get("cache_write_tokens")),
                    "thinking_tokens": _as_int(reviewer_result.get("thinking_budget")),
                }
                reviewer_items.append(reviewer_payload)
                totals["cache_read_tokens"] += reviewer_payload["cache_read_tokens"]
                totals["cache_write_tokens"] += reviewer_payload["cache_write_tokens"]
                totals["thinking_tokens"] += reviewer_payload["thinking_tokens"]
            phase_payload["reviewers"] = reviewer_items

            reviewer_errors = result.get("errors", {})
            if reviewer_errors:
                phase_payload["reviewer_errors"] = reviewer_errors

        phases_payload.append(phase_payload)

    totals["duration_seconds"] = round(totals["duration_seconds"], 3)
    return {
        "report_generated_at": datetime.now().isoformat(),
        "run_id": run.run_id,
        "run_title": run.title,
        "run_created_at": run.created_at,
        "overall_status": run.overall_status,
        "totals": totals,
        "phases": phases_payload,
    }


def _write_token_report_yaml(run) -> Path:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML ist nicht installiert. Bitte `pip install pyyaml` ausfuehren.") from exc

    cfg = _load_config()
    save_dir = _cfg_dir(cfg, "token_reports_dir", "data/token_reports")
    save_dir.mkdir(parents=True, exist_ok=True)

    report = _build_token_report_dict(run)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"token_report_{_safe_title(run.title)}_{run.run_id[:8]}_{ts}.yaml"
    target = save_dir / filename
    target.write_text(
        yaml.safe_dump(report, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return target


# ── Page Routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    runs = sm.list_runs()
    api_keys = _get_api_keys()
    has_any_key = any(v for v in api_keys.values())
    return render_template("index.html", runs=runs, has_any_key=has_any_key)


@app.route("/run/<run_id>")
def run_view(run_id: str):
    run = sm.load_run(run_id)
    if run is None:
        return "Run nicht gefunden.", 404
    cfg = _load_config()
    reviewers = cfg.get("reviewer", {})
    km_choices = cfg.get("teamleiter_choices", [])
    km_default = cfg.get("teamleiter", {})
    api_keys = _get_api_keys()
    masked_keys = {k: ("***" if v else "") for k, v in api_keys.items()}
    return render_template(
        "run.html",
        run=run.to_dict(),
        reviewers=reviewers,
        masked_keys=masked_keys,
        km_choices=km_choices,
        km_default=km_default,
    )


@app.route("/run/new", methods=["POST"])
def new_run():
    title = request.form.get("title", "").strip() or f"Run {datetime.now().strftime('%d.%m %H:%M')}"
    description = request.form.get("description", "").strip()
    input_text = request.form.get("input_text", "").strip()
    km_system_prompt = request.form.get("km_system_prompt", "").strip()
    context_sheet = request.form.get("context_sheet", "").strip()
    skip_phase1 = request.form.get("skip_phase1") == "1"

    run = sm.create_run(
        title=title,
        description=description,
        input_text=input_text,
        km_system_prompt=km_system_prompt,
        context_sheet=context_sheet,
    )
    capture_telemetry("run_created", {"skip_phase1": skip_phase1, "has_input": bool(input_text)})

    # If a finished review YAML is provided, auto-approve Phase 1.
    if skip_phase1 and input_text:
        sm.mark_phase_running(run.run_id, 0)
        sm.mark_phase_review(run.run_id, 0, {
            "yaml_text": input_text,
            "skipped": True,
            "note": "Direkt eingefuegt — Phase 1 uebersprungen",
        })
        sm.approve_phase(run.run_id, 0)

    return redirect(url_for("run_view", run_id=run.run_id))


# ── API Keys ─────────────────────────────────────────────────────────────────

@app.route("/api/keys", methods=["POST"])
def save_keys():
    data = request.get_json(force=True)
    current = session.get("api_keys", {})
    for k in ("google", "anthropic", "openai", "deepseek"):
        val = data.get(k, "").strip()
        if val:
            current[k] = val
    session["api_keys"] = current
    session.modified = True
    return jsonify({"ok": True})


@app.route("/api/keys/status")
def keys_status():
    api_keys = _get_api_keys()
    return jsonify({k: bool(v) for k, v in api_keys.items()})


@app.route("/api/language", methods=["POST"])
def set_language():
    body = request.get_json(force=True, silent=True) or {}
    lang = get_language(body.get("lang"))
    response = jsonify({"ok": True, "lang": lang})
    response.set_cookie("lang", lang, max_age=60 * 60 * 24 * 365, samesite="Lax")
    return response


@app.route("/api/onboarding/status")
def onboarding_status():
    cfg = _load_config()
    api_keys = _get_api_keys()
    providers = sorted({v.get("provider", "anthropic") for v in cfg.get("reviewer", {}).values()})
    orchestrator_provider = cfg.get("teamleiter", {}).get("provider", "anthropic")
    return jsonify({
        "has_any_key": any(api_keys.values()),
        "demo_available": "demo" in providers or orchestrator_provider == "demo",
        "providers": providers,
        "orchestrator_provider": orchestrator_provider,
    })


# ── Run Status ───────────────────────────────────────────────────────────────

@app.route("/api/run/<run_id>/status")
def run_status(run_id: str):
    run = sm.load_run(run_id)
    if run is None:
        return jsonify({"error": "not found"}), 404

    data = run.to_dict()

    # Merge live reviewer progress for phase 2 if running
    review_state = get_review_state(run_id)
    if review_state is not None:
        data["live_review"] = review_state

    return jsonify(data)


# ── Phase Actions ─────────────────────────────────────────────────────────────

@app.route("/api/run/<run_id>/phase/<int:phase_idx>/start", methods=["POST"])
def phase_start(run_id: str, phase_idx: int):
    run = sm.load_run(run_id)
    if run is None:
        return jsonify({"error": "Run nicht gefunden"}), 404

    phase = run.phases[phase_idx]
    if phase.status not in ("pending", "rejected"):
        return jsonify({"error": f"Phase ist {phase.status}, kann nicht gestartet werden."}), 400

    # Ensure all previous phases are approved before starting this one
    if phase_idx > 0:
        for i in range(phase_idx):
            if run.phases[i].status != "approved":
                return jsonify({
                    "error": f"Phase {i + 1} muss zuerst freigegeben werden (aktuell: {run.phases[i].status})."
                }), 400

    api_keys = _get_api_keys()
    body = request.get_json(force=True, silent=True) or {}

    # Update api_keys from request if provided
    for k in ("google", "anthropic", "openai", "deepseek"):
        val = body.get(f"key_{k}", "").strip()
        if val:
            api_keys[k] = val

    km_provider     = body.get("km_provider")     or None
    km_model        = body.get("km_model")        or None
    thinking_effort = body.get("thinking_effort") or "none"
    temperature     = body.get("temperature")
    if temperature is not None:
        temperature = float(temperature)

    sm.mark_phase_running(run_id, phase_idx)

    def _run_phase():
        try:
            if phase_idx == 0:
                _execute_phase1(run_id, run, api_keys, body, km_provider, km_model, thinking_effort, temperature)
            elif phase_idx == 1:
                _execute_phase2(run_id, run, api_keys, body, thinking_effort, temperature)
            elif phase_idx == 2:
                _execute_phase3(run_id, run, api_keys, km_provider, km_model, thinking_effort, temperature)
            elif phase_idx == 3:
                _execute_phase4(run_id, run, api_keys, km_provider, km_model, thinking_effort, temperature)
            elif phase_idx == 4:
                _execute_phase5(run_id, run)
        except Exception as e:
            sm.mark_phase_error(run_id, phase_idx, str(e))

    threading.Thread(target=_run_phase, daemon=True).start()
    return jsonify({"ok": True, "status": "running"})


def _km_label(km_model: str | None, km_provider: str | None) -> str:
    """Return a display label for the KM model, falling back to config default."""
    if km_model:
        return km_model
    cfg = _load_config().get("teamleiter", {})
    return cfg.get("model", "claude-sonnet-4-6")


def _execute_phase1(run_id: str, run, api_keys: dict, body: dict,
                    km_provider: str | None = None, km_model: str | None = None,
                    thinking_effort: str = "none", temperature: float | None = None):
    content = run.input_text or ""
    if not content:
        content = body.get("content", "")
    result = generate_review_yaml(
        description=run.description,
        content=content,
        api_keys=api_keys,
        km_provider=km_provider,
        km_model=km_model,
        km_system_prompt=run.km_system_prompt or None,
        thinking_effort=thinking_effort,
        temperature=temperature,
    )
    if result["success"]:
        sm.mark_phase_review(run_id, 0, {
            "yaml_text": result["yaml_text"],
            "duration": result["duration"],
            "km_model": _km_label(km_model, km_provider),
            "km_provider": km_provider,
            "thinking_effort": thinking_effort,
            "thinking_budget": result.get("thinking_budget", 0),
            "cache_read_tokens":  result.get("cache_read_tokens", 0),
            "cache_write_tokens": result.get("cache_write_tokens", 0),
        })
    else:
        sm.mark_phase_error(run_id, 0, result["error"])


def _execute_phase2(run_id: str, run, api_keys: dict, body: dict,
                    thinking_effort: str = "none", temperature: float | None = None):
    phase1_result = run.phases[0].result or {}
    review_yaml = phase1_result.get("yaml_text", run.input_text or "")
    selected = body.get("selected_reviewers") or None

    run_parallel_reviews(
        run_id, review_yaml, selected, api_keys,
        context_sheet=run.context_sheet or "",
        thinking_effort=thinking_effort,
        temperature=temperature,
    )

    while not is_review_complete(run_id):
        time.sleep(1)

    results = get_review_results(run_id)
    clear_review_state(run_id)

    # Add total duration = longest reviewer (they run in parallel)
    reviewer_durations = [
        r.get("duration", 0) or 0
        for r in results.get("results", {}).values()
    ]
    results["duration"] = round(max(reviewer_durations), 1) if reviewer_durations else 0
    results["thinking_effort"] = thinking_effort
    try:
        auto_path = _save_phase2_report(run, results)
        results["auto_saved_path"] = str(auto_path)
    except Exception as exc:
        APP_LOGGER.exception("Auto-save for phase 2 failed (run_id=%s)", run_id)
        results["auto_save_error"] = str(exc)

    sm.mark_phase_review(run_id, 1, results)


def _execute_phase3(run_id: str, run, api_keys: dict,
                    km_provider: str | None = None, km_model: str | None = None,
                    thinking_effort: str = "none", temperature: float | None = None):
    phase1_result = run.phases[0].result or {}
    phase2_result = run.phases[1].result or {}
    review_yaml = phase1_result.get("yaml_text", "")
    raw_results = phase2_result.get("results", {})
    reviews = {v.get("ki_name", k): v.get("text", "") for k, v in raw_results.items()}

    result = consolidate_reviews(
        review_yaml=review_yaml,
        reviews=reviews,
        api_keys=api_keys,
        km_provider=km_provider,
        km_model=km_model,
        km_system_prompt=run.km_system_prompt or None,
        thinking_effort=thinking_effort,
        temperature=temperature,
    )
    if result["success"]:
        phase_result = {
            "consolidation_text": result["consolidation_text"],
            "duration": result["duration"],
            "km_model": _km_label(km_model, km_provider),
            "km_provider": km_provider,
            "thinking_effort": thinking_effort,
            "thinking_budget": result.get("thinking_budget", 0),
            "cache_read_tokens":  result.get("cache_read_tokens", 0),
            "cache_write_tokens": result.get("cache_write_tokens", 0),
        }
        try:
            auto_path = _save_phase3_report(run, phase_result)
            phase_result["auto_saved_path"] = str(auto_path)
        except Exception as exc:
            APP_LOGGER.exception("Auto-save for phase 3 failed (run_id=%s)", run_id)
            phase_result["auto_save_error"] = str(exc)
        sm.mark_phase_review(run_id, 2, phase_result)
    else:
        sm.mark_phase_error(run_id, 2, result["error"])


def _execute_phase4(run_id: str, run, api_keys: dict,
                    km_provider: str | None = None, km_model: str | None = None,
                    thinking_effort: str = "none", temperature: float | None = None):
    phase3_result = run.phases[2].result or {}
    consolidation = phase3_result.get("consolidation_text", "")

    result = generate_cursor_task(
        consolidation=consolidation,
        title=run.title,
        api_keys=api_keys,
        km_provider=km_provider,
        km_model=km_model,
        km_system_prompt=run.km_system_prompt or None,
        thinking_effort=thinking_effort,
        temperature=temperature,
    )
    if result["success"]:
        sm.mark_phase_review(run_id, 3, {
            "task_yaml": result["task_markdown"],
            "duration": result["duration"],
            "km_model": _km_label(km_model, km_provider),
            "km_provider": km_provider,
            "thinking_effort": thinking_effort,
            "thinking_budget": result.get("thinking_budget", 0),
            "cache_read_tokens":  result.get("cache_read_tokens", 0),
            "cache_write_tokens": result.get("cache_write_tokens", 0),
        })
    else:
        sm.mark_phase_error(run_id, 3, result["error"])


def _execute_phase5(run_id: str, run):
    phase4_result = run.phases[3].result or {}
    # Support both old "task_markdown" key and new "task_yaml"
    task_content = phase4_result.get("task_yaml") or phase4_result.get("task_markdown", "")

    result = write_cursor_task(task_content, title=run.title)
    if result["success"]:
        sm.mark_phase_review(run_id, 4, {
            "file_path": result["file_path"],
            "filename": result["filename"],
        })
    else:
        sm.mark_phase_error(run_id, 4, result["error"])


@app.route("/api/run/<run_id>/phase/<int:phase_idx>/approve", methods=["POST"])
def phase_approve(run_id: str, phase_idx: int):
    body = request.get_json(force=True, silent=True) or {}
    edited_result = body.get("result")
    # Merge edited text into existing result to preserve metadata (km_model, duration, cache tokens, …)
    if edited_result is not None:
        existing = sm.load_run(run_id)
        base_result = existing.phases[phase_idx].result if existing else None
        if isinstance(base_result, dict) and isinstance(edited_result, dict):
            edited_result = {**base_result, **edited_result}
    run = sm.approve_phase(run_id, phase_idx, edited_result=edited_result)
    if run is None:
        return jsonify({"error": "Run nicht gefunden"}), 404
    capture_telemetry("phase_approved", {"phase_idx": phase_idx, "overall_status": run.overall_status})
    response: dict[str, Any] = {"ok": True, "run": run.to_dict()}
    if phase_idx == 4 and run.overall_status == "completed":
        try:
            token_report_path = _write_token_report_yaml(run)
            response["token_report_path"] = str(token_report_path)
        except Exception as exc:
            APP_LOGGER.exception("Auto token report creation failed (run_id=%s)", run_id)
            response["token_report_error"] = str(exc)
    return jsonify(response)


@app.route("/api/run/<run_id>/phase/<int:phase_idx>/reject", methods=["POST"])
def phase_reject(run_id: str, phase_idx: int):
    body = request.get_json(force=True, silent=True) or {}
    reason = body.get("reason", "")
    run = sm.reject_phase(run_id, phase_idx, reason=reason)
    if run is None:
        return jsonify({"error": "Run nicht gefunden"}), 404
    return jsonify({"ok": True, "run": run.to_dict()})


@app.route("/api/run/<run_id>/delete", methods=["POST"])
def run_delete(run_id: str):
    deleted = sm.delete_run(run_id)
    if not deleted:
        return jsonify({"error": "Run nicht gefunden"}), 404
    return jsonify({"ok": True})


@app.route("/api/run/<run_id>/abort", methods=["POST"])
def run_abort(run_id: str):
    run = sm.abort_run(run_id)
    if run is None:
        return jsonify({"error": "Run nicht gefunden"}), 404
    return jsonify({"ok": True, "run": run.to_dict()})


@app.route("/api/run/<run_id>/phase/<int:phase_idx>/cancel", methods=["POST"])
def phase_cancel(run_id: str, phase_idx: int):
    """Cancel a running phase — resets to pending."""
    run = sm.cancel_phase(run_id, phase_idx)
    if run is None:
        return jsonify({"error": "Run nicht gefunden"}), 404
    return jsonify({"ok": True, "run": run.to_dict()})


@app.route("/api/run/<run_id>/phase/<int:phase_idx>/retry", methods=["POST"])
def phase_retry(run_id: str, phase_idx: int):
    run = sm.retry_phase(run_id, phase_idx)
    if run is None:
        return jsonify({"error": "Run nicht gefunden"}), 404
    return jsonify({"ok": True, "run": run.to_dict()})


@app.route("/api/run/<run_id>/phase/<int:phase_idx>/update", methods=["POST"])
def phase_update(run_id: str, phase_idx: int):
    body = request.get_json(force=True, silent=True) or {}
    incoming_raw = body.get("result", {})
    incoming = incoming_raw if isinstance(incoming_raw, dict) else {}
    # Merge edited text into existing result to preserve metadata (km_model, duration, cache tokens, …)
    existing = sm.load_run(run_id)
    base_result = existing.phases[phase_idx].result if existing else None
    if isinstance(base_result, dict):
        result = {**base_result, **incoming}
    else:
        result = incoming
    run = sm.update_phase_result(run_id, phase_idx, result)
    if run is None:
        return jsonify({"error": "Run nicht gefunden"}), 404
    return jsonify({"ok": True})


# ── Phase 2: Export ──────────────────────────────────────────────────────────

@app.route("/api/run/<run_id>/phase/1/export")
def phase2_export(run_id: str):
    """Download Phase-2 reviewer results as a single Markdown file."""
    run = sm.load_run(run_id)
    if run is None:
        return "Run nicht gefunden", 404
    phase = run.phases[1]
    if not phase.result:
        return "Keine Ergebnisse vorhanden", 404

    results = phase.result.get("results", {})
    errors  = phase.result.get("errors", {})

    lines = [
        f"# Phase-2 Review-Ergebnisse: {run.title}",
        f"Run-ID: {run.run_id}",
        f"Datum: {run.created_at[:10]}",
        f"Reviewer: {len(results)} erfolgreich, {len(errors)} Fehler",
        "",
    ]
    for ki_id, r in results.items():
        lines += [
            "---",
            f"## {r.get('ki_name', ki_id)}",
            f"**Rolle:** {r.get('role', '—')}  |  **Dauer:** {r.get('duration', '?')}s",
            "",
            r.get("text", ""),
            "",
        ]
    if errors:
        lines += ["---", "## Fehler", ""]
        for ki_id, e in errors.items():
            lines.append(f"- **{e.get('ki_name', ki_id)}:** {e.get('error', '?')}")

    content = "\n".join(lines)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_title = "".join(c if c.isalnum() or c in "-_" else "_" for c in run.title)[:40]
    filename = f"review_ergebnisse_{safe_title}_{ts}.md"

    from flask import Response
    return Response(
        content,
        mimetype="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Phase 2: Save to Disk ─────────────────────────────────────────────────────

@app.route("/api/run/<run_id>/phase/1/save", methods=["POST"])
def phase2_save(run_id: str):
    """Save Phase-2 results as Markdown into review_results_dir."""
    run = sm.load_run(run_id)
    if run is None:
        return jsonify({"error": "Run nicht gefunden"}), 404
    phase = run.phases[1]
    if not phase.result:
        return jsonify({"error": "Keine Ergebnisse vorhanden"}), 404

    try:
        target = _save_phase2_report(run, phase.result)
    except Exception as exc:
        return jsonify({"error": f"Speichern fehlgeschlagen: {exc}"}), 500
    return jsonify({"ok": True, "filename": target.name, "path": str(target)})


@app.route("/api/run/<run_id>/phase/2/save", methods=["POST"])
def phase3_save(run_id: str):
    """Save Phase-3 consolidation as Markdown into konsolidierungen_dir."""
    run = sm.load_run(run_id)
    if run is None:
        return jsonify({"error": "Run nicht gefunden"}), 404
    phase = run.phases[2]
    if not phase.result:
        return jsonify({"error": "Keine Konsolidierung vorhanden"}), 404
    try:
        target = _save_phase3_report(run, phase.result)
    except Exception as exc:
        return jsonify({"error": f"Speichern fehlgeschlagen: {exc}"}), 500
    return jsonify({"ok": True, "filename": target.name, "path": str(target)})


@app.route("/api/run/<run_id>/token_report", methods=["POST"])
def run_token_report(run_id: str):
    """Write a YAML token report for a run."""
    run = sm.load_run(run_id)
    if run is None:
        return jsonify({"error": "Run nicht gefunden"}), 404
    try:
        target = _write_token_report_yaml(run)
    except Exception as exc:
        return jsonify({"error": f"Token-Report fehlgeschlagen: {exc}"}), 500
    return jsonify({"ok": True, "filename": target.name, "path": str(target)})


# ── Phase 2: Live Reviewer Progress ──────────────────────────────────────────

@app.route("/api/run/<run_id>/review_progress")
def review_progress(run_id: str):
    state = get_review_state(run_id)
    if state is None:
        return jsonify({"running": False, "results": {}, "errors": {}, "done_count": 0, "total_count": 0})
    return jsonify(state)


# ── File Utilities (from orchester.py) ───────────────────────────────────────

@app.route("/api/docs/files")
def docs_files():
    """List files from the configured documents folder (Systemprompt, Current State Sheet, etc.)."""
    cfg = _load_config()
    docs_path = _cfg_dir(cfg, "docs_dir", "data/systemprompts")
    if not docs_path.exists():
        return jsonify([])
    exts = {".yaml", ".yml", ".md", ".txt"}
    files = []
    try:
        for entry in sorted(docs_path.iterdir(), key=lambda e: e.name):
            if entry.is_file() and entry.suffix.lower() in exts and not entry.name.startswith("."):
                files.append({
                    "name": entry.name,
                    "path": str(entry),
                    "size_kb": round(entry.stat().st_size / 1024, 1),
                })
    except Exception:
        pass
    return jsonify(files)


@app.route("/api/auftraege")
def list_auftraege():
    try:
        cfg = _load_config()
        auftraege_dir = _cfg_dir(cfg, "auftraege_dir", "data/review_auftraege")
        files: list[Path] = []
        for pattern in ("*.yaml", "*.yml", "*.md", "*.txt"):
            files.extend(auftraege_dir.glob(pattern))
        files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        return jsonify([{"name": f.name, "path": str(f)} for f in files])
    except Exception:
        return jsonify([])


@app.route("/api/read_file")
def read_file():
    path = request.args.get("path", "")
    try:
        text = Path(path).read_text(encoding="utf-8")
        return text, 200, {"Content-Type": "text/plain; charset=utf-8"}
    except Exception as e:
        return str(e), 400


# ── Workspace Browser ─────────────────────────────────────────────────────

def _allowed_extensions() -> list[str]:
    cfg = _load_config()
    return cfg.get("workspace_browser", {}).get(
        "extensions", [".py", ".md", ".yaml", ".yml", ".txt", ".ts", ".js"]
    )


def _is_allowed_path(path: Path) -> bool:
    """Only allow browsing within configured workspace roots."""
    cfg = _load_config()
    roots = _workspace_root_paths(cfg)
    try:
        resolved = path.resolve()
        return any(
            resolved == r.resolve() or resolved.is_relative_to(r.resolve())
            for r in roots
        )
    except Exception:
        return False


@app.route("/api/workspace/roots")
def workspace_roots():
    cfg = _load_config()
    meta = cfg.get("workspace_roots", [])
    resolved = _workspace_root_paths(cfg)
    result = []
    for r, p in zip(meta, resolved):
        result.append({
            "label": r.get("label", p.name),
            "path": str(p),
            "exists": p.exists(),
        })
    return jsonify(result)


@app.route("/api/workspace/list")
def workspace_list():
    """List files and subdirectories in a path (security-checked)."""
    dir_path = Path(request.args.get("path", ""))
    if not _is_allowed_path(dir_path):
        return jsonify({"error": "Pfad nicht erlaubt"}), 403
    if not dir_path.exists() or not dir_path.is_dir():
        return jsonify({"error": "Verzeichnis nicht gefunden"}), 404

    exts = set(_allowed_extensions())
    dirs, files = [], []
    try:
        for entry in sorted(dir_path.iterdir(), key=lambda e: (e.is_file(), e.name.lower())):
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                dirs.append({"name": entry.name, "path": str(entry), "type": "dir"})
            elif entry.is_file() and entry.suffix.lower() in exts:
                size = entry.stat().st_size
                files.append({
                    "name": entry.name,
                    "path": str(entry),
                    "type": "file",
                    "size": size,
                    "size_kb": round(size / 1024, 1),
                })
    except PermissionError:
        return jsonify({"error": "Kein Lesezugriff"}), 403

    return jsonify({
        "path": str(dir_path),
        "parent": str(dir_path.parent) if _is_allowed_path(dir_path.parent) else None,
        "dirs": dirs,
        "files": files,
    })


@app.route("/api/workspace/read_multi", methods=["POST"])
def workspace_read_multi():
    """Read and concatenate multiple files."""
    body = request.get_json(force=True, silent=True) or {}
    paths = body.get("paths", [])
    separator = body.get("separator", "\n\n---\n\n")
    parts = []
    errors = []
    for p_str in paths:
        p = Path(p_str)
        if not _is_allowed_path(p):
            errors.append(f"{p.name}: nicht erlaubt")
            continue
        try:
            content = p.read_text(encoding="utf-8")
            parts.append(f"# Datei: {p.name}\n\n{content}")
        except Exception as e:
            errors.append(f"{p.name}: {e}")
    return jsonify({
        "content": separator.join(parts),
        "loaded": len(parts),
        "errors": errors,
    })


# ── Orchestrator Model Choices (technischer Route-Name bleibt erhalten) ─────

@app.route("/api/konzertmeister_models")
@app.route("/api/teamleiter_models")
def konzertmeister_models():
    cfg = _load_config()
    choices = cfg.get("teamleiter_choices", [])
    default_cfg = cfg.get("teamleiter", {})
    default = {
        "provider": default_cfg.get("provider", "anthropic"),
        "model":    default_cfg.get("model", "claude-sonnet-4-6"),
    }
    return jsonify({"choices": choices, "default": default})


# ── Statistics ────────────────────────────────────────────────────────────────

@app.route("/api/stats")
def get_stats():
    """Aggregate statistics across all runs for the dashboard."""
    cfg = _load_config()
    reviewer_cfg = cfg.get("reviewer", {})

    # Build model-id -> display-label map from teamleiter choices
    _model_label_map = {
        c["model"]: c["label"]
        for c in cfg.get("teamleiter_choices", [])
        if "model" in c and "label" in c
    }

    # Counters
    run_totals   = {"total": 0, "completed": 0, "running": 0, "aborted": 0, "pending": 0, "review": 0, "rejected": 0}
    phase_approvals   = [{"approved": 0, "rejected": 0} for _ in range(5)]
    phase_durations: list[list[float]] = [[] for _ in range(5)]
    model_usage: dict[str, int] = {}   # model_label -> count
    reviewer_usage: dict[str, int] = {}   # ki_name -> count

    # Cache/token stats keyed by display-name
    cache_by_ki = {}         # name -> {read: int, write: int, calls: int}

    def _add_cache(name: str, read: int, write: int):
        if name not in cache_by_ki:
            cache_by_ki[name] = {"read": 0, "write": 0, "calls": 0}
        cache_by_ki[name]["read"]  += read
        cache_by_ki[name]["write"] += write
        cache_by_ki[name]["calls"] += 1

    thinking_tokens_total = 0
    total_run_duration    = []

    data_dir = Path(__file__).parent / "data"
    seen_ids: set[str] = set()

    for f in sorted(data_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            run = sm.load_run_file(f)
            if run is None or run.run_id in seen_ids:
                continue
            seen_ids.add(run.run_id)
        except Exception:
            continue

        run_totals["total"] += 1
        status = run.overall_status
        if status == "completed":
            run_totals["completed"] += 1
        elif status == "running":
            run_totals["running"] += 1
        elif status in ("pending",):
            run_totals["pending"] += 1
        elif status == "review":
            run_totals["review"] += 1
        elif status == "rejected":
            run_totals["aborted"] += 1
        else:
            run_totals["aborted"] += 1

        run_dur = 0.0
        for i, phase in enumerate(run.phases):
            r = phase.result or {}

            # Phase durations
            dur = r.get("duration", 0) or 0
            if dur:
                phase_durations[i].append(float(dur))
                run_dur += float(dur)

            # Approve / reject counts
            if phase.status == "approved":
                phase_approvals[i]["approved"] += 1
            elif phase.status == "rejected":
                phase_approvals[i]["rejected"] += 1

            # KM phases (0, 2, 3): model usage + cache
            # Skip phases that were bypassed (skip_phase1) — no real KM call happened
            if i in (0, 2, 3) and r and not r.get("skipped"):
                km_model = r.get("km_model") or r.get("model", "")
                if km_model:
                    km_label = _model_label_map.get(km_model, km_model)
                    model_usage[km_label] = model_usage.get(km_label, 0) + 1
                cache_read  = r.get("cache_read_tokens", 0) or 0
                cache_write = r.get("cache_write_tokens", 0) or 0
                if cache_read or cache_write:
                    label = _model_label_map.get(km_model, km_model) if km_model else "Orchestrator"
                    _add_cache(label, cache_read, cache_write)
                thinking_tokens_total += r.get("thinking_budget", 0) or 0

            # Phase 2: per-reviewer cache + usage + thinking tokens
            if i == 1 and r:
                results = r.get("results", {})
                for ki_id, rv in results.items():
                    ki_name = rv.get("ki_name") or reviewer_cfg.get(ki_id, {}).get("name", ki_id)
                    reviewer_usage[ki_name] = reviewer_usage.get(ki_name, 0) + 1
                    cache_read  = rv.get("cache_read_tokens", 0) or 0
                    cache_write = rv.get("cache_write_tokens", 0) or 0
                    _add_cache(ki_name, cache_read, cache_write)
                    thinking_tokens_total += rv.get("thinking_budget", 0) or 0

        if run_dur:
            total_run_duration.append(run_dur)

    # Averages
    avg_phase_dur = [
        round(sum(d) / len(d), 1) if d else None
        for d in phase_durations
    ]
    avg_run_dur = round(sum(total_run_duration) / len(total_run_duration), 1) if total_run_duration else None

    # Approve rates
    approve_rates = []
    for pa in phase_approvals:
        total_decided = pa["approved"] + pa["rejected"]
        approve_rates.append(
            round(pa["approved"] / total_decided * 100) if total_decided else None
        )

    # Total cache tokens
    total_cache_read  = sum(v["read"]  for v in cache_by_ki.values())
    total_cache_write = sum(v["write"] for v in cache_by_ki.values())

    return jsonify({
        "runs": run_totals,
        "cache": {
            "total_read":  total_cache_read,
            "total_write": total_cache_write,
            "by_ki":       cache_by_ki,
        },
        "tokens": {
            "thinking_total": thinking_tokens_total,
        },
        "phases": {
            "avg_duration_s": avg_phase_dur,
            "approve_rates":  approve_rates,
            "approvals":      phase_approvals,
        },
        "avg_run_duration_s": avg_run_dur,
        "model_usage":   model_usage,
        "reviewer_usage": reviewer_usage,
    })


# ── Reviewer Config ───────────────────────────────────────────────────────────

@app.route("/api/reviewers")
def list_reviewers():
    cfg = _load_config()
    reviewers = cfg.get("reviewer", {})
    return jsonify({
        k: {"name": v.get("name", k), "role": v.get("role", ""), "provider": v.get("provider", "")}
        for k, v in reviewers.items()
    })


def main() -> None:
    host = os.environ.get("REVIEWLOOP_HOST", "localhost")
    port = int(os.environ.get("REVIEWLOOP_PORT", "5000"))
    url = f"http://localhost:{port}"
    print("\n  ReviewLoop")
    print(f"  Browser: {url}")
    print("  Stoppen: Ctrl+C\n")
    if host in ("localhost", "127.0.0.1"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    app.run(host=host, port=port, debug=False, threaded=True)


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    main()
