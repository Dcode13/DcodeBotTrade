"""Notifier Telegram (push alert real-time) via HTTP API langsung (§12.3).

Sengaja TIDAK memakai library async agar sederhana. Jika token/chat kosong
(mode dev), pesan dialihkan ke log -> bot tetap jalan untuk alert-only lokal.
"""

from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot{token}/{method}"


class Notifier:
    def __init__(self, token: str, owner_chat_id: str, timeout: int = 10) -> None:
        self.token = token
        self.owner_chat_id = owner_chat_id
        self.timeout = timeout
        self.enabled = bool(token and owner_chat_id)
        if not self.enabled:
            log.warning("Telegram nonaktif (token/chat_id kosong). Alert -> log.")

    def send(self, text: str, parse_mode: str | None = None) -> bool:
        """Kirim pesan ke OWNER_CHAT_ID. Tidak pernah melempar (fail-safe)."""
        if not self.enabled:
            log.info("[ALERT] %s", text)
            return False
        url = API_BASE.format(token=self.token, method="sendMessage")
        payload: dict[str, object] = {
            "chat_id": self.owner_chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            resp = requests.post(url, json=payload, timeout=self.timeout)
            if resp.status_code != 200:
                log.error("Telegram sendMessage gagal %s: %s", resp.status_code, resp.text[:200])
                return False
            return True
        except requests.RequestException as exc:
            log.error("Telegram error: %s", exc)
            return False
