#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Adapter seams for the two outside providers the engine talks to.

  LLMProvider       -> scores leads and drafts outreach email (task prompts are
                       transport-agnostic; adapters only move bytes).
  BusinessSource    -> finds candidate businesses for a niche.

A provider adapter is PURE: every piece of config it needs (key, base_url,
model, login) is passed into its constructor/call — nothing is read from
import-time module globals. That is what makes providers swappable AND
seller-scoped (two sellers on different providers coexist in one process).
"""

import abc


class LLMProvider(abc.ABC):
    """One LLM backend. `.chat` is the single transport primitive; the shared
    task layer (llm.py) builds prompts and parses replies on top of it."""

    name = "abstract"

    @abc.abstractmethod
    def chat(self, messages, *, temperature=0.2, json_mode=False):
        """Send an OpenAI-style [{'role','content'}, ...] message list to the
        model and return the assistant's text. `json_mode` asks for a strict
        JSON object reply where the backend supports it. Raises on failure."""
        raise NotImplementedError


class BusinessSource(abc.ABC):
    """One FIND backend returning candidate business rows."""

    name = "abstract"

    @abc.abstractmethod
    def find_businesses(self, keyword, place, *, location_name=None,
                        location_code=None, max_results=10):
        """Return a list of business rows in the standard maps schema (the same
        shape dataforseo.map_item produces)."""
        raise NotImplementedError
