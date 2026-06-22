"""Bot Telegram: polling perintah + auth (§12).

Memakai HTTP API langsung (getUpdates) sehingga bisa dipanggil non-blocking
dari loop utama (§6 langkah 1) tanpa thread/async.

KEAMANAN (§12.1): hanya merespons ``OWNER_CHAT_ID``. Pesan dari ID lain
diabaikan (di-log saja).
"""

from __future__ import annotations

import logging
from typing import Callable

import requests

from telegram.notifier import Notifier

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot{token}/{method}"

# handler(command_tanpa_slash, args) -> teks balasan
CommandHandler = Callable[[str, list[str]], str]


class TelegramBot:
    def __init__(
        self,
        token: str,
        owner_chat_id: str,
        notifier: Notifier,
        handler: CommandHandler,
        timeout: int = 10,
    ) -> None:
        self.token = token
        self.owner_chat_id = str(owner_chat_id)
        self.notifier = notifier
        self.handler = handler
        self.timeout = timeout
        self._offset: int | None = None
        self.enabled = bool(token and owner_chat_id)

    # ------------------------------------------------------------------ #
    def poll_and_process(self) -> None:
        """Ambil update baru (non-blocking) & proses perintah owner."""
        if not self.enabled:
            return
        updates = self._get_updates()
        for upd in updates:
            self._offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message")
            if not msg:
                continue
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text = msg.get("text", "")
            if chat_id != self.owner_chat_id:
                log.warning("Pesan dari chat tak dikenal %s diabaikan.", chat_id)
                continue
            if not text:
                continue
            self._dispatch(text.strip())

    # ------------------------------------------------------------------ #
    def _get_updates(self) -> list[dict]:
        url = API_BASE.format(token=self.token, method="getUpdates")
        params: dict[str, object] = {"timeout": 0}
        if self._offset is not None:
            params["offset"] = self._offset
        try:
            resp = requests.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                log.error("getUpdates !ok: %s", data)
                return []
            return data.get("result", [])
        except (requests.RequestException, ValueError) as exc:
            log.error("getUpdates error: %s", exc)
            return []

    # ------------------------------------------------------------------ #
    def _dispatch(self, text: str) -> None:
        parts = text.split()
        cmd = parts[0].lstrip("/").split("@")[0].lower()
        args = parts[1:]
        try:
            reply = self.handler(cmd, args)
        except Exception as exc:  # noqa: BLE001 - jangan biarkan perintah crash loop
            log.exception("Handler perintah '%s' error", cmd)
            reply = f"⚠️ Error memproses /{cmd}: {exc}"
        if reply:
            self.notifier.send(reply)
