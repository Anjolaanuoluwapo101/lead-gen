#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
OpenAI-compatible LLM transport.

Covers Groq, OpenAI, OpenRouter, Together and local vLLM/Ollama servers — they
all speak the /chat/completions protocol. Because this transport is shared,
"swapping" between them is only a matter of base_url + api_key + model (config),
not code. A per-seller key that is NOT on an OpenAI-compatible provider (e.g.
native Anthropic) should use the AnthropicProvider adapter instead.
"""

from ._transport import llm_session
from .base import LLMProvider


class OpenAICompatible(LLMProvider):
    name = "openai_compatible"

    def __init__(self, *, api_key, base_url=None, model=None):
        if not api_key:
            raise RuntimeError(
                f"Missing api_key for provider '{self.name}'. Configure it per "
                "seller or set the matching master key in .env.")
        self.api_key = api_key
        # normalize: strip a trailing '/v1' if someone passes the site root.
        base = (base_url or "https://api.openai.com/v1").strip().rstrip("/")
        self.base_url = base if base.endswith("/v1") else base + "/v1"
        self.model = model or "openai/gpt-oss-120b"

    def chat(self, messages, *, temperature=0.2, json_mode=False):
        payload = {"model": self.model, "temperature": temperature,
                   "messages": messages}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        resp = llm_session().post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            json=payload, timeout=90,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
