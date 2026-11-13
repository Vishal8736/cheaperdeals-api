from __future__ import annotations

import logging

import httpx

log = logging.getLogger("cheapedeal.telegram")


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.token, self.chat_id = token, chat_id

    def send(self, text: str) -> bool:
        if not (self.token and self.chat_id):
            return False
        try:
            r = httpx.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                           json={"chat_id": self.chat_id, "text": text[:3900], "disable_web_page_preview": True}, timeout=10)
            return r.status_code == 200
        except Exception as e:  # alerts must never crash the pipeline
            log.warning("telegram send failed: %s", e)
            return False


class NullNotifier:
    def send(self, text: str) -> bool:
        return False


class MemoryNotifier:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, text: str) -> bool:
        self.sent.append(text)
        return True
