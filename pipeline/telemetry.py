from __future__ import annotations

import json
import os
import threading
import urllib.request
from typing import Any


def enabled() -> bool:
    return bool(os.environ.get("REVIEWLOOP_TELEMETRY_URL"))


def capture(event: str, properties: dict[str, Any] | None = None) -> None:
    """
    Opt-in telemetry hook.

    Nothing is sent unless REVIEWLOOP_TELEMETRY_URL is set. Payloads
    intentionally avoid prompts, file paths, API keys, and model outputs.
    """
    url = os.environ.get("REVIEWLOOP_TELEMETRY_URL", "").strip()
    if not url:
        return

    payload = {
        "event": event,
        "properties": properties or {},
        "source": "reviewloop",
    }

    def _send() -> None:
        try:
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=2)
        except Exception:
            return

    threading.Thread(target=_send, daemon=True).start()
