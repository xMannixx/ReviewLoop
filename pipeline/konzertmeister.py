"""
Orchestrator — API role for phases 1, 3, and 4.

Phase 1: Generate structured review YAML from description + file contents
Phase 3: Consolidate parallel review results (consensus, unique findings, priorities)
Phase 4: Generate Cursor task YAML from consolidated findings

Prompts use XML tags (<context>, <code>, <instructions>, <constraints>) as
recommended by Anthropic for reliable structured parsing.
"""

from __future__ import annotations

import os
from pathlib import Path

from pipeline.api_clients import call_llm
from pipeline.config_alias import dashboard_config_path, load_dashboard_config


def _fmt(template: str, **kwargs) -> str:
    """Safe template substitution — values are never interpreted as format strings."""
    result = template
    for key, value in kwargs.items():
        result = result.replace("{" + key + "}", str(value))
    return result


def _get_config() -> dict:
    config_path = dashboard_config_path(Path(__file__).parent.parent)
    cfg = load_dashboard_config(config_path)
    return cfg.get("teamleiter", {})


def _get_api_key(api_keys: dict, provider: str | None = None) -> str:
    if provider is None:
        cfg = _get_config()
        provider = cfg.get("provider", "anthropic")
    env_map = {
        "anthropic": "ANTHROPIC_API_KEY",
        "google":    "GOOGLE_API_KEY",
        "openai":    "OPENAI_API_KEY",
        "deepseek":  "DEEPSEEK_API_KEY",
    }
    return api_keys.get(provider, "") or os.environ.get(env_map.get(provider, "ANTHROPIC_API_KEY"), "")


# ── Phase 1: Review-Auftrag erstellen ────────────────────────────────────────

PHASE1_PROMPT_TEMPLATE = """\
<context>
Aufgabenbeschreibung des Nutzers:
{description}
</context>

<code>
{content}
</code>

<instructions>
Erstelle einen strukturierten Review-Auftrag im YAML-Format fuer die oben stehenden Dateien/den Code.

Generiere ein YAML mit folgender Struktur:
```yaml
auftrag:
  titel: "<praegnanter Titel>"
  beschreibung: "<Was soll reviewt werden und warum>"
  prioritaet: hoch|mittel|niedrig

review_fokus:
  - bereich: "<Fokusbereich>"
    fragen:
      - "<Konkrete Review-Frage>"

qualitaetskriterien:
  - "<Messbares Kriterium>"

erwartetes_ergebnis: "<Was soll am Ende herauskommen>"
```
</instructions>

<constraints>
- Antworte NUR mit dem YAML
- Kein Prosa davor oder danach
- Keine Markdown-Codeblock-Marker
</constraints>"""


def generate_review_yaml(
    description: str,
    content: str,
    api_keys: dict,
    km_provider: str | None = None,
    km_model: str | None = None,
    km_system_prompt: str | None = None,
    thinking_effort: str = "none",
    temperature: float | None = None,
) -> dict:
    cfg = _get_config()
    provider = km_provider or cfg.get("provider", "anthropic")
    model    = km_model    or cfg.get("model", "claude-sonnet-4-6")
    base_prompt = km_system_prompt or cfg.get("system_prompt", "")
    anti_mogel  = cfg.get("anti_mogel", "")
    system_prompt = (base_prompt + "\n" + anti_mogel).strip() if anti_mogel else base_prompt
    effective_temp = temperature if temperature is not None else cfg.get("temperature", 0.3)

    prompt = _fmt(
        PHASE1_PROMPT_TEMPLATE,
        description=description or "(keine spezifische Beschreibung)",
        content=content[:12000] if content else "(kein Inhalt angegeben)",
    )

    result = call_llm(
        provider=provider, model=model, prompt=prompt,
        api_key=_get_api_key(api_keys, provider),
        system_prompt=system_prompt,
        temperature=effective_temp,
        max_tokens=cfg.get("max_tokens", 8192),
        thinking_effort=thinking_effort,
    )

    if result["success"]:
        yaml_text = result["text"].strip()
        for fence in ("```yaml", "```"):
            if yaml_text.startswith(fence):
                yaml_text = yaml_text[len(fence):]
        if yaml_text.endswith("```"):
            yaml_text = yaml_text[:-3]
        yaml_text = yaml_text.strip()
        return {"success": True, "yaml_text": yaml_text, "error": None,
                "duration": result["duration"],
                "cache_read_tokens":  result.get("cache_read_tokens", 0),
                "cache_write_tokens": result.get("cache_write_tokens", 0),
                "thinking_budget":    result.get("thinking_budget", 0)}
    else:
        return {"success": False, "yaml_text": "", "error": result["error"],
                "duration": result["duration"],
                "cache_read_tokens": 0, "cache_write_tokens": 0, "thinking_budget": 0}


# ── Phase 3: Konsolidierung ───────────────────────────────────────────────────

PHASE3_PROMPT_TEMPLATE = """\
<context>
Review-Auftrag (zur Orientierung):
{review_yaml}
</context>

<reviews>
{reviews}
</reviews>

<instructions>
Du hast die oben stehenden parallelen Review-Ergebnisse von verschiedenen KI-Reviewern erhalten.
Konsolidiere sie zu einem umfassenden Bericht mit folgender Struktur:

## Konsens-Findings
(Punkte die mehrere Reviewer unabhaengig gefunden haben — hoechste Prioritaet)

## Einzigartige Findings
(Wichtige Punkte die nur ein Reviewer gefunden hat)

## Priorisierte Massnahmen
(Nummerierte Liste: Was soll Cursor als naechstes tun? Konkret und umsetzbar)

## Zusammenfassung
(2-3 Saetze: Gesamtzustand und wichtigste Massnahme)
</instructions>

<constraints>
- Praezise und handlungsorientiert
- Auf Deutsch
- Keine Wiederholung von offensichtlichem
</constraints>"""


def consolidate_reviews(
    review_yaml: str,
    reviews: dict[str, str],
    api_keys: dict,
    km_provider: str | None = None,
    km_model: str | None = None,
    km_system_prompt: str | None = None,
    thinking_effort: str = "none",
    temperature: float | None = None,
) -> dict:
    cfg = _get_config()
    provider = km_provider or cfg.get("provider", "anthropic")
    model    = km_model    or cfg.get("model", "claude-sonnet-4-6")
    base_prompt = km_system_prompt or cfg.get("system_prompt", "")
    anti_mogel  = cfg.get("anti_mogel", "")
    system_prompt = (base_prompt + "\n" + anti_mogel).strip() if anti_mogel else base_prompt
    effective_temp = temperature if temperature is not None else cfg.get("temperature", 0.3)

    reviews_text = ""
    for ki_name, text in reviews.items():
        reviews_text += f"\n\n<review reviewer=\"{ki_name}\">\n{text}\n</review>"

    prompt = _fmt(
        PHASE3_PROMPT_TEMPLATE,
        review_yaml=review_yaml or "(kein Auftrag)",
        reviews=reviews_text.strip(),
    )

    result = call_llm(
        provider=provider, model=model, prompt=prompt,
        api_key=_get_api_key(api_keys, provider),
        system_prompt=system_prompt,
        temperature=effective_temp,
        max_tokens=cfg.get("max_tokens", 8192),
        thinking_effort=thinking_effort,
    )

    if result["success"]:
        return {"success": True, "consolidation_text": result["text"], "error": None,
                "duration": result["duration"],
                "cache_read_tokens":  result.get("cache_read_tokens", 0),
                "cache_write_tokens": result.get("cache_write_tokens", 0),
                "thinking_budget":    result.get("thinking_budget", 0)}
    else:
        return {"success": False, "consolidation_text": "", "error": result["error"],
                "duration": result["duration"],
                "cache_read_tokens": 0, "cache_write_tokens": 0, "thinking_budget": 0}


# ── Phase 4: Cursor-Auftrag generieren ───────────────────────────────────────

PHASE4_PROMPT_TEMPLATE = """\
<context>
Konsolidierte Review-Analyse:
{consolidation}
</context>

<instructions>
Erstelle basierend auf der obigen Analyse einen strukturierten Cursor-Auftrag als YAML.
Antworte NUR mit validem YAML, kein Text davor oder danach.

```yaml
titel: "{title}"
prioritaet: hoch|mittel|niedrig
kontext: |
  (1-2 Saetze: Was wurde reviewt und was ist das Ziel)
aufgaben:
  - id: 1
    titel: "(praegnanter Aufgabentitel)"
    dateien:
      - "(betroffene Datei wenn bekannt)"
    problem: |
      (Was ist das Problem — konkret)
    loesung: |
      (Was soll Cursor tun — konkret und umsetzbar)
    prioritaet: hoch|mittel|niedrig
akzeptanzkriterien:
  - "(Wie weiss Cursor dass die Aufgabe erledigt ist)"
hinweise: |
  (Constraints und Anti-Mogel-Regeln)
```
</instructions>

<constraints>
- Nur YAML, kein Prosa
- Multiline-Strings mit | (Literal Block Scalar)
- Dateinamen soweit bekannt aus dem Review ableiten
- Maximal 8 Aufgaben, priorisiert nach Konsens-Findings zuerst
- Im hinweise-Abschnitt IMMER enthalten:
  * Keine hardcodierten Werte oder loesungen die nur fuer spezifische Test-Inputs funktionieren
  * Falls Tests falsch sind: Nutzer informieren statt Tests umgehen
  * Fail-Closed: bei Unsicherheit ablehnen, nicht raten
</constraints>"""


def generate_cursor_task(
    consolidation: str,
    title: str,
    api_keys: dict,
    km_provider: str | None = None,
    km_model: str | None = None,
    km_system_prompt: str | None = None,
    thinking_effort: str = "none",
    temperature: float | None = None,
) -> dict:
    cfg = _get_config()
    provider = km_provider or cfg.get("provider", "anthropic")
    model    = km_model    or cfg.get("model", "claude-sonnet-4-6")
    base_prompt = km_system_prompt or cfg.get("system_prompt", "")
    anti_mogel  = cfg.get("anti_mogel", "")
    system_prompt = (base_prompt + "\n" + anti_mogel).strip() if anti_mogel else base_prompt
    effective_temp = temperature if temperature is not None else cfg.get("temperature", 0.3)

    prompt = _fmt(
        PHASE4_PROMPT_TEMPLATE,
        consolidation=consolidation,
        title=title,
    )

    result = call_llm(
        provider=provider, model=model, prompt=prompt,
        api_key=_get_api_key(api_keys, provider),
        system_prompt=system_prompt,
        temperature=effective_temp,
        max_tokens=cfg.get("max_tokens", 8192),
        thinking_effort=thinking_effort,
    )

    if result["success"]:
        return {"success": True, "task_markdown": result["text"], "error": None,
                "duration": result["duration"],
                "cache_read_tokens":  result.get("cache_read_tokens", 0),
                "cache_write_tokens": result.get("cache_write_tokens", 0),
                "thinking_budget":    result.get("thinking_budget", 0)}
    else:
        return {"success": False, "task_markdown": "", "error": result["error"],
                "duration": result["duration"],
                "cache_read_tokens": 0, "cache_write_tokens": 0, "thinking_budget": 0}
