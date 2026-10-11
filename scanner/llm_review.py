"""Optional qualitative notes from a locally installed LLM via Ollama (free, runs on your Mac).

Disabled by default. Never changes the rule-based score; output goes to the
`llm_notes` column only, labelled as model opinion. If Ollama is not running the
scan continues without it.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)

PROMPT = """You are helping an SEO researcher review a blog article. In at most 3 short bullet points,
note concrete weaknesses in depth, evidence or specificity. Do not guess who or what wrote it and do
not claim it is AI-generated. If it looks fine, say so.

Title: {title}

Article (truncated):
{text}
"""


class OllamaReviewer:
    def __init__(self, cfg: dict | None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled"))
        self.url = (cfg.get("url") or "http://localhost:11434").rstrip("/")
        self.model = cfg.get("model") or "llama3.1"
        self.timeout = float(cfg.get("timeout_seconds", 60))
        host = urlsplit(self.url).hostname or ""
        if host not in ("localhost", "127.0.0.1", "::1"):
            log.warning("Ollama URL must be local; disabling LLM notes")
            self.enabled = False
        self.available = self.enabled and self._ping()

    def _ping(self) -> bool:
        try:
            return requests.get(self.url + "/api/tags", timeout=3).ok
        except requests.RequestException:
            log.info("Ollama not reachable at %s; LLM notes skipped", self.url)
            return False

    def review(self, title: str, text: str) -> str:
        if not self.available:
            return ""
        try:
            resp = requests.post(self.url + "/api/generate", json={
                "model": self.model, "prompt": PROMPT.format(title=title, text=text[:6000]), "stream": False,
                "options": {"temperature": 0},
            }, timeout=self.timeout)
            resp.raise_for_status()
            return "LLM opinion (" + self.model + "): " + (resp.json().get("response") or "").strip()[:800]
        except (requests.RequestException, ValueError) as exc:
            log.info("Ollama review failed: %s", exc)
            return ""
