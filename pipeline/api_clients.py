"""
Generic LLM dispatcher.
All providers use urllib.request (no SDK), consistent with orchester.py.

Prompt Caching:
- Anthropic: cache_control ephemeral on system + last user block (beta header).
  Cache read ~10 % of base input price. 5m write ~1.25x base; optional 1h ~2x base
  (config [anthropic] prompt_cache_ttl = "5m" | "1h").
  Min. cacheable: ~1024 tok (Sonnet 4.6), ~4096 (Opus 4.6).
- OpenAI o3/o1/o4: Automatic prefix caching (no config needed).
  IMPORTANT: temperature must NOT be sent for reasoning models.
- DeepSeek R1: Automatic disk-based KV-cache, no changes needed.
  API returns usage.prompt_cache_hit_tokens.
- Google Gemini: Implicit prefix caching (automatic, no config needed).
  systemInstruction is sent via the dedicated field (not as a user message)
  so Gemini can treat it as a stable cacheable prefix.
  Cached tokens are reported in usageMetadata.cachedContentTokenCount.
  Minimum cacheable prefix: ~1 024–2 048 tokens (model-dependent).
  Cached tokens cost 25 % of normal input price.
"""

import json
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from pipeline.config_alias import dashboard_config_path

# OpenAI models that do NOT accept the temperature parameter
_OPENAI_REASONING_MODELS = ("o1", "o3", "o4")


def _anthropic_ephemeral_cache_control() -> dict:
    """
    Read [anthropic] prompt_cache_ttl from config.toml.
    "5m" (default): {"type": "ephemeral"} — standard 5-minute cache write pricing.
    "1h":           {"type": "ephemeral", "ttl": "1h"} — 2x write, 1h retention.
    """
    try:
        p = dashboard_config_path(Path(__file__).resolve().parent.parent)
        with open(p, "rb") as f:
            t = tomllib.load(f).get("anthropic", {})
        ttl = str(t.get("prompt_cache_ttl", "5m")).strip().lower()
        if ttl in ("1h", "1 hour", "3600", "60m"):
            return {"type": "ephemeral", "ttl": "1h"}
    except Exception:
        pass
    return {"type": "ephemeral"}


def call_llm(
    provider: str,
    model: str,
    prompt: str,
    api_key: str,
    system_prompt: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 8192,
    thinking_effort: str = "none",
) -> dict:
    """
    Unified entry point for all LLM providers.
    Returns: {success, text, error, duration, cache_read_tokens, cache_write_tokens}
    """
    fn = {
        "demo":      _call_demo,
        "google":    _call_google,
        "anthropic": _call_anthropic,
        "openai":    _call_openai,
        "deepseek":  _call_deepseek,
    }.get(provider)

    if fn is None:
        return {
            "success": False, "text": "", "error": f"Unbekannter Provider: {provider}",
            "duration": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
        }

    return fn(
        model=model,
        prompt=prompt,
        api_key=api_key,
        system_prompt=system_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
        thinking_effort=thinking_effort,
    )


# ── Demo Provider ───────────────────────────────────────────────────────────

def _call_demo(
    model: str, prompt: str, api_key: str,
    system_prompt: str | None = None,
    temperature: float = 0.2, max_tokens: int = 8192,
    thinking_effort: str = "none",
) -> dict:
    """Deterministic offline provider for first-run demos and screenshots."""
    start = time.time()
    if "konsolid" in prompt.lower() or "review-ergebnisse" in prompt.lower():
        text = """# Demo-Konsolidierung

## Konsens-Findings
- Eingaben brauchen klare Validierung und gut sichtbare Fehlerzustaende.
- Lange Konfigurationen sollten in Defaults und optionale Details getrennt werden.

## Priorisierte Massnahmen
1. Quickstart vereinfachen.
2. Erste Erfolgserfahrung per Demo-Modus sicherstellen.
3. Review-Ausgaben exportierbar machen."""
    elif "cursor" in prompt.lower() or "auftrag" in prompt.lower():
        text = """titel: Demo: Quickstart verbessern
aufgaben:
  - problem: "Neue Nutzer brauchen zu viele manuelle Schritte."
    loesung: "Quickstart, Demo-Modus und klarere Fehlermeldungen ergaenzen."
    betroffene_dateien:
      - README.md
      - templates/index.html
akzeptanzkriterien:
  - "Ein Nutzer kann den Demo-Flow ohne API-Key starten."
  - "Fehler werden als Toast statt Alert angezeigt."
hinweise:
  - "Dies ist ein Demo-Provider-Ergebnis." """
    else:
        text = """title: Demo Review Brief
description: "Automatically generated demo brief for ReviewLoop."
context:
  goal: "Show the full 5-phase workflow without external API keys."
acceptance_criteria:
  - "The run can proceed through all phases."
  - "Outputs are structured and editable."
review_steps:
  - "Check onboarding clarity."
  - "Check error handling and empty states."
priority: medium"""
    return {
        "success": True,
        "text": text,
        "error": None,
        "duration": round(time.time() - start, 2),
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "thinking_budget": 0,
    }


# ── Google Gemini ─────────────────────────────────────────────────────────────

def _call_google(
    model: str, prompt: str, api_key: str,
    system_prompt: str | None = None,
    temperature: float = 0.2, max_tokens: int = 8192,
    thinking_effort: str = "none",
) -> dict:
    """
    Gemini caller with thinking-budget control and cache reporting.

    thinking_effort:
      "none"   -> no thinkingConfig sent (model default; some models like
                  gemini-3.x-pro-preview REQUIRE thinking, budget=0 is invalid)
      "low"    -> thinkingBudget=1024
      "medium" -> thinkingBudget=8000
      "high"   -> thinkingBudget=24576
      "dynamic"-> thinkingBudget=-1  (model decides)

    Cache: systemInstruction is sent via the dedicated field so Gemini treats
    it as a stable cacheable prefix. Cache hits reported in
    usageMetadata.cachedContentTokenCount.
    """
    _EFFORT_BUDGET = {"low": 1024, "medium": 8000, "high": 24576, "dynamic": -1}
    thinking_budget = _EFFORT_BUDGET.get(thinking_effort)

    start = time.time()
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )

    gen_config: dict = {"temperature": temperature, "maxOutputTokens": max_tokens}
    if thinking_budget is not None:
        gen_config["thinkingConfig"] = {"thinkingBudget": thinking_budget}

    payload: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": gen_config,
    }
    if system_prompt:
        payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}

    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read())
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            usage = data.get("usageMetadata", {})
            cache_read     = usage.get("cachedContentTokenCount", 0)
            thinking_tokens = usage.get("thoughtsTokenCount", 0)
            return {"success": True, "text": text, "error": None,
                    "duration": round(time.time() - start, 1),
                    "cache_read_tokens": cache_read, "cache_write_tokens": 0,
                    "thinking_budget": thinking_tokens}
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")[:300]
        return {"success": False, "text": "", "error": f"HTTP {e.code}: {err}",
                "duration": round(time.time() - start, 1),
                "cache_read_tokens": 0, "cache_write_tokens": 0, "thinking_budget": 0}
    except Exception as e:
        return {"success": False, "text": "", "error": str(e),
                "duration": round(time.time() - start, 1),
                "cache_read_tokens": 0, "cache_write_tokens": 0, "thinking_budget": 0}


# ── Anthropic Claude ──────────────────────────────────────────────────────────

def _call_anthropic(
    model: str, prompt: str, api_key: str,
    system_prompt: str | None = None,
    temperature: float = 0.3, max_tokens: int = 8192,
    thinking_effort: str = "none",
) -> dict:
    """
    Prompt caching + Extended Thinking (adaptive for Opus 4.6, budget for Sonnet 4.6).

    thinking_effort:
      "none"   → no thinking (default, fastest)
      "low"    → light thinking
      "medium" → moderate thinking
      "high"   → deep thinking
      "max"    → maximum thinking (Opus 4.6 only)

    Opus 4.6 (claude-opus-4-6):
      Uses adaptive thinking: {type: "adaptive"} — Claude decides when to think.
      Depth controlled via effort parameter (GA, no beta header needed).
      Prefills NOT supported — we don't use any.

    Sonnet 4.6 (claude-sonnet-4-6):
      Uses legacy thinking: {type: "enabled", budget_tokens: N}.
      Still supported, will migrate when Anthropic deprecates it.

    When thinking is active:
    - temperature is forced to 1 (Anthropic requirement)
    - max_tokens is auto-expanded if needed
    - Response may contain thinking blocks — only text blocks are returned

    Cache economics (Sonnet 4.6, per 1M tokens):
      Normal input: $3.00 → Cache write: $3.75 → Cache read: $0.30
    """
    _is_opus = "opus" in model.lower()
    _EFFORT_BUDGET_SONNET = {"none": 0, "low": 1024, "medium": 8000, "high": 16000, "max": 32000}

    start = time.time()
    url = "https://api.anthropic.com/v1/messages"

    wants_thinking = thinking_effort and thinking_effort != "none"

    if _is_opus:
        # Opus 4.6: adaptive thinking + effort parameter
        effective_max_tokens = max(max_tokens, 16384) if wants_thinking else max_tokens
        effective_temperature = 1 if wants_thinking else temperature

        payload = {
            "model": model,
            "max_tokens": effective_max_tokens,
            "temperature": effective_temperature,
        }
        if wants_thinking:
            payload["thinking"] = {"type": "adaptive", "effort": thinking_effort}
        budget_for_stats = 0  # Opus manages its own budget
    else:
        # Sonnet 4.6: legacy budget_tokens mode
        budget = _EFFORT_BUDGET_SONNET.get(thinking_effort, 0)
        effective_max_tokens = max(max_tokens, budget + 4096) if budget > 0 else max_tokens
        effective_temperature = 1 if budget > 0 else temperature

        payload = {
            "model": model,
            "max_tokens": effective_max_tokens,
            "temperature": effective_temperature,
        }
        if budget > 0:
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
        budget_for_stats = budget

    _cc = _anthropic_ephemeral_cache_control()
    if system_prompt:
        payload["system"] = [
            {"type": "text", "text": system_prompt, "cache_control": _cc}
        ]

    payload["messages"] = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt, "cache_control": _cc},
            ],
        }
    ]

    # Prompt caching beta header; interleaved-thinking header is ignored by Opus 4.6
    # but still needed for Sonnet 4.6 with budget_tokens
    betas = "prompt-caching-2024-07-31"
    if not _is_opus and wants_thinking:
        betas += ",interleaved-thinking-2025-05-14"

    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": betas,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            data = json.loads(resp.read())
            text = "".join(
                b.get("text", "") for b in data.get("content", [])
                if b.get("type") == "text"
            )
            usage = data.get("usage", {})
            cache_read  = usage.get("cache_read_input_tokens", 0)
            cache_write = usage.get("cache_creation_input_tokens", 0)
            # For stats: Opus reports actual thinking tokens used, Sonnet uses budget
            thinking_used = budget_for_stats
            if _is_opus:
                # Sum thinking tokens from content blocks
                thinking_used = sum(
                    len(b.get("thinking", ""))
                    for b in data.get("content", [])
                    if b.get("type") == "thinking"
                ) or usage.get("output_tokens", 0) - len(text)
                if thinking_used < 0:
                    thinking_used = 0
            return {"success": True, "text": text, "error": None,
                    "duration": round(time.time() - start, 1),
                    "cache_read_tokens": cache_read,
                    "cache_write_tokens": cache_write,
                    "thinking_budget": thinking_used}
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")[:300]
        return {"success": False, "text": "", "error": f"HTTP {e.code}: {err}",
                "duration": round(time.time() - start, 1),
                "cache_read_tokens": 0, "cache_write_tokens": 0,
                "thinking_budget": budget_for_stats}
    except Exception as e:
        return {"success": False, "text": "", "error": str(e),
                "duration": round(time.time() - start, 1),
                "cache_read_tokens": 0, "cache_write_tokens": 0,
                "thinking_budget": budget_for_stats}


# ── OpenAI ────────────────────────────────────────────────────────────────────

def _call_openai(
    model: str, prompt: str, api_key: str,
    system_prompt: str | None = None,
    temperature: float = 0.2, max_tokens: int = 8192,
    thinking_effort: str = "none",
) -> dict:
    """
    Automatic prompt caching on GPT-4o and o3 (no config needed).

    IMPORTANT: o1 / o3 / o4 reasoning models do NOT accept the temperature
    parameter — sending it causes a 400 error. Detected and stripped here.

    Caching is maximised by putting static content first (context sheet,
    system prompt) which is already the case in our prompt construction.
    """
    start = time.time()
    url = "https://api.openai.com/v1/chat/completions"

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    payload: dict = {
        "model": model,
        "max_completion_tokens": max_tokens,
        "messages": messages,
    }

    # Reasoning models (o1, o3, o4-*) reject the temperature parameter entirely
    is_reasoning = model.startswith(_OPENAI_REASONING_MODELS)
    if not is_reasoning:
        payload["temperature"] = temperature

    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read())
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            cache_read = usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
            return {"success": True, "text": text, "error": None,
                    "duration": round(time.time() - start, 1),
                    "cache_read_tokens": cache_read,
                    "cache_write_tokens": 0}
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")[:300]
        return {"success": False, "text": "", "error": f"HTTP {e.code}: {err}",
                "duration": round(time.time() - start, 1),
                "cache_read_tokens": 0, "cache_write_tokens": 0}
    except Exception as e:
        return {"success": False, "text": "", "error": str(e),
                "duration": round(time.time() - start, 1),
                "cache_read_tokens": 0, "cache_write_tokens": 0}


# ── DeepSeek ──────────────────────────────────────────────────────────────────

def _call_deepseek(
    model: str, prompt: str, api_key: str,
    system_prompt: str | None = None,
    temperature: float = 0.2, max_tokens: int = 8192,
    thinking_effort: str = "none",
) -> dict:
    """
    DeepSeek R1 has automatic disk-based KV-cache — no explicit config needed.
    Repeated identical prefixes (same context sheet + code) are cached server-side.
    The API returns cache usage in usage.prompt_cache_hit_tokens.
    """
    start = time.time()
    url = "https://api.deepseek.com/v1/chat/completions"

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": messages,
    }).encode()

    req = urllib.request.Request(
        url, data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read())
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            cache_read = usage.get("prompt_cache_hit_tokens", 0)
            return {"success": True, "text": text, "error": None,
                    "duration": round(time.time() - start, 1),
                    "cache_read_tokens": cache_read,
                    "cache_write_tokens": 0}
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")[:300]
        return {"success": False, "text": "", "error": f"HTTP {e.code}: {err}",
                "duration": round(time.time() - start, 1),
                "cache_read_tokens": 0, "cache_write_tokens": 0}
    except Exception as e:
        return {"success": False, "text": "", "error": str(e),
                "duration": round(time.time() - start, 1),
                "cache_read_tokens": 0, "cache_write_tokens": 0}
