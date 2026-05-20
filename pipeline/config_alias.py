from __future__ import annotations

import copy
import logging
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

_deprecation_logged = False


def dashboard_config_path(repo_root: Path | None = None) -> Path:
    """
    Prefer `config.toml`; if missing, use `config.example.toml` (fresh clone).
    """
    root = repo_root if repo_root is not None else REPO_ROOT
    primary = root / "config.toml"
    if primary.exists():
        return primary
    fallback = root / "config.example.toml"
    if fallback.exists():
        return fallback
    return primary


def resolve_project_path(raw: str | None, *, default_relative: str) -> Path:
    """
    Paths in config: relative → anchored at repository root; absolute → unchanged.
    """
    text = (raw or "").strip()
    if not text:
        text = default_relative
    p = Path(text)
    if p.is_absolute():
        return p
    return (REPO_ROOT / p).resolve()


def reset_alias_warning_for_tests() -> None:
    """Reset one-time deprecation log flag (tests only)."""
    global _deprecation_logged
    _deprecation_logged = False


def _warn_deprecated_once(logger: logging.Logger | None, message: str) -> None:
    global _deprecation_logged
    if _deprecation_logged:
        return
    _deprecation_logged = True
    (logger or logging.getLogger("reviewloop.dashboard")).warning(message)


def normalize_teamleiter_aliases(
    cfg: dict[str, Any],
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Normalize teamleiter/konzertmeister config keys with teamleiter priority."""
    has_teamleiter = isinstance(cfg.get("teamleiter"), dict)
    has_konzertmeister = isinstance(cfg.get("konzertmeister"), dict)
    has_teamleiter_choices = isinstance(cfg.get("teamleiter_choices"), list)
    has_konzertmeister_choices = isinstance(cfg.get("konzertmeister_choices"), list)

    if (has_konzertmeister and not has_teamleiter) or (
        has_konzertmeister_choices and not has_teamleiter_choices
    ):
        _warn_deprecated_once(
            logger,
            "Deprecated config keys [konzertmeister]/[[konzertmeister_choices]] in use. "
            "Please migrate to [teamleiter]/[[teamleiter_choices]].",
        )

    selected_teamleiter = (
        cfg["teamleiter"] if has_teamleiter else cfg.get("konzertmeister", {}) or {}
    )
    selected_choices = (
        cfg["teamleiter_choices"]
        if has_teamleiter_choices
        else cfg.get("konzertmeister_choices", []) or []
    )

    cfg["teamleiter"] = copy.deepcopy(selected_teamleiter)
    cfg["konzertmeister"] = copy.deepcopy(selected_teamleiter)
    cfg["teamleiter_choices"] = copy.deepcopy(selected_choices)
    cfg["konzertmeister_choices"] = copy.deepcopy(selected_choices)
    return cfg


def load_dashboard_config(config_path: Path, logger: logging.Logger | None = None) -> dict[str, Any]:
    with open(config_path, "rb") as f:
        raw = tomllib.load(f)
    return normalize_teamleiter_aliases(raw, logger=logger)
