"""
Phase 2: Parallel Reviews.
Adapted from orchester.py run_all_reviews() — one thread per reviewer.
"""

from __future__ import annotations

import os
import threading
import tomllib
from pathlib import Path
from typing import Any

from pipeline.api_clients import call_llm
from pipeline.config_alias import dashboard_config_path


def _load_reviewer_configs() -> dict[str, dict]:
    """Load all [reviewer.*] sections from config.toml."""
    config_path = dashboard_config_path(Path(__file__).parent.parent)
    with open(config_path, "rb") as f:
        cfg = tomllib.load(f)
    return cfg.get("reviewer", {})


def _resolve_api_key(provider: str, api_keys: dict) -> str:
    env_map = {
        "google":    "GOOGLE_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "openai":    "OPENAI_API_KEY",
        "deepseek":  "DEEPSEEK_API_KEY",
    }
    return api_keys.get(provider, "") or os.environ.get(env_map.get(provider, ""), "")


REVIEW_SYSTEM_PROMPT_TEMPLATE = """\
Du bist ein erfahrener Software-Reviewer mit dem Fokus: {role}.
Reviewe den folgenden Code/die folgenden Dateien gruendlich aus deiner Perspektive.
Sei praezise, konkret und handlungsorientiert. Nutze Markdown-Formatierung."""


# ── Global parallel review state ─────────────────────────────────────────────
# Keyed by run_id — multiple runs can be in flight simultaneously
_states: dict[str, dict] = {}
_lock = threading.Lock()


def get_review_state(run_id: str) -> dict | None:
    with _lock:
        return _states.get(run_id)


def run_parallel_reviews(
    run_id: str,
    prompt: str,
    selected_reviewers: list[str] | None,
    api_keys: dict,
    context_sheet: str = "",
    thinking_effort: str = "none",
    temperature: float | None = None,
) -> None:
    """
    Launch one review thread per selected reviewer.
    State is accessible via get_review_state(run_id).
    selected_reviewers: list of reviewer keys (e.g. ["google", "anthropic"]) or None for all.
    thinking_effort: passed to each provider's call_llm (controls thinking budget).
    temperature: overrides config temperature if set.
    """
    configs = _load_reviewer_configs()
    if selected_reviewers:
        configs = {k: v for k, v in configs.items() if k in selected_reviewers}

    skipped_errors: dict[str, dict[str, Any]] = {}
    runnable_configs: dict[str, dict] = {}
    for ki_id, cfg in configs.items():
        provider = cfg.get("provider", "anthropic")
        if provider == "demo" or _resolve_api_key(provider, api_keys):
            runnable_configs[ki_id] = cfg
        else:
            skipped_errors[ki_id] = {
                "ki_name": cfg.get("name", ki_id),
                "error": f"API-Key fuer {provider} fehlt; Reviewer wurde uebersprungen.",
                "duration": 0,
            }
    configs = runnable_configs

    if not configs:
        with _lock:
            _states[run_id] = {
                "running": False,
                "results": {},
                "errors": skipped_errors or {
                    "_": {
                        "ki_name": "Reviewer",
                        "error": "Keine Reviewer ausgewaehlt oder konfiguriert.",
                        "duration": 0,
                    }
                },
                "done_count": 0,
                "total_count": 0,
                "done_names": [],
            }
        return

    # Prepend Current State Sheet to the review prompt if provided
    effective_prompt = prompt
    if context_sheet and context_sheet.strip():
        effective_prompt = (
            "## Project Current State Sheet\n"
            "(Zur Halluzinationskontrolle — bitte beim Review beachten)\n\n"
            + context_sheet.strip()
            + "\n\n---\n\n"
            + prompt
        )

    with _lock:
        _states[run_id] = {
            "running": True,
            "results": {},
            "errors": skipped_errors,
            "done_count": 0,
            "total_count": len(configs),
            "done_names": [],
        }

    def _worker(ki_id: str, cfg: dict) -> None:
        provider = cfg.get("provider", "anthropic")
        model = cfg.get("model", "")
        ki_name = cfg.get("name", ki_id)
        role = cfg.get("role", "Allgemeiner Review")
        system_prompt = REVIEW_SYSTEM_PROMPT_TEMPLATE.replace("{role}", role)
        api_key = _resolve_api_key(provider, api_keys)

        effective_temp = temperature if temperature is not None else cfg.get("temperature", 0.2)

        result = call_llm(
            provider=provider,
            model=model,
            prompt=effective_prompt,
            api_key=api_key,
            system_prompt=system_prompt,
            temperature=effective_temp,
            max_tokens=cfg.get("max_tokens", 8192),
            thinking_effort=thinking_effort,
        )

        with _lock:
            state = _states.get(run_id)
            if state is None:
                return
            if result["success"]:
                state["results"][ki_id] = {
                    "ki_name": ki_name,
                    "text": result["text"],
                    "duration": result["duration"],
                    "role": role,
                    "cache_read_tokens":  result.get("cache_read_tokens", 0),
                    "cache_write_tokens": result.get("cache_write_tokens", 0),
                    "thinking_budget":    result.get("thinking_budget", 0),
                }
            else:
                state["errors"][ki_id] = {
                    "ki_name": ki_name,
                    "error": result["error"],
                    "duration": result["duration"],
                }
            state["done_count"] += 1
            state["done_names"].append(ki_name)
            if state["done_count"] >= state["total_count"]:
                state["running"] = False

    for ki_id, cfg in configs.items():
        threading.Thread(
            target=_worker,
            args=(ki_id, cfg),
            daemon=True,
        ).start()


def is_review_complete(run_id: str) -> bool:
    state = get_review_state(run_id)
    if state is None:
        return False
    return not state.get("running", True)


def get_review_results(run_id: str) -> dict[str, Any]:
    """Return results dict suitable for storage in Phase.result."""
    state = get_review_state(run_id)
    if state is None:
        return {}
    return {
        "results": state.get("results", {}),
        "errors": state.get("errors", {}),
        "done_count": state.get("done_count", 0),
        "total_count": state.get("total_count", 0),
    }


def clear_review_state(run_id: str) -> None:
    with _lock:
        _states.pop(run_id, None)
