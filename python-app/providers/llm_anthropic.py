#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Native Anthropic LLM transport (Messages API), for when a seller uses Anthropic
directly. NOTE: this adapter is structural — it mirrors the OpenAI-compatible
shape but is NOT live-tested in this environment (no Anthropic key is present).
Treat it as the reference for adding a genuinely different transport; verify
against a real key before relying on it. Anthropic is only reachable here, not
Groq, which uses the OpenAICompatible adapter.
"""

import requests

from .base import LLMProvider


class Anthropic(LLMProvider):
    name = "anthropic"

    def __init__(self, *, api_key, base_url=None, model=None):
        if not api_key:
            raise RuntimeError("Missing api_key for provider 'anthropic'.")
        self.api_key = api_key
        self.base_url = (base_url or "https://api.anthropic.com").strip().rstrip("/")
        self.model = model or "claude-3-5-haiku-latest"

    def chat(self, messages, *, temperature=0.2, json_mode=False):
        # Anthropic wants system separated out; json_mode is best-effort here
        # (Anthropic has no response_format) — the task layer's tolerant parser
        # still recovers the JSON object from prose.
        system = "\n".join(m["content"] for m in messages
                           if m["role"] == "system")
        user_msgs = [{"role": m["role"], "content": m["content"]}
                     for m in messages if m["role"] != "system"]
        payload = {
            "model": self.model,
            "max_tokens": 1024,
            "temperature": temperature,
            "system": system,
            "messages": user_msgs,
        }
        resp = requests.post(
            f"{self.base_url}/v1/messages",
            headers={"x-api-key": self.api_key,
                     "anthropic-version": "2023-06-01",
                     "Content-Type": "application/json"},
            json=payload, timeout=90,
        )
        resp.raise_for_status()
        data = resp.json()
        return "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text")
