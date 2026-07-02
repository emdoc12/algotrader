"""Thin Anthropic client + a manual tool-use loop for the trading agents.

Uses the official `anthropic` SDK. Each agent is a model + system prompt +
a set of client-side tools whose handlers we execute locally (place a paper
trade, journal a lesson, file a dev request, …). We run the manual agentic
loop so we can log every tool call, enforce an iteration cap, and handle the
`refusal` stop reason explicitly.

Requires ANTHROPIC_API_KEY in the environment at runtime.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Callable

DEFAULT_MODEL = os.environ.get("AGENT_MODEL", "claude-opus-4-8")


@dataclass
class AgentResult:
    text: str
    actions: list[dict] = field(default_factory=list)   # [{tool, input, result}]
    refused: bool = False
    error: str | None = None
    usage: dict = field(default_factory=dict)  # {input_tokens, output_tokens, cached_input_tokens}


class Agent:
    """One LLM agent: a persona + toolbox, driven by a pluggable provider.

    The provider (Claude, OpenAI, Grok, Qwen, …) owns the actual tool-use loop;
    the Agent just binds the persona, tools, and handlers and delegates. This is
    what lets four different models run the identical desk in the competition.
    """

    def __init__(
        self,
        name: str,
        system: str,
        tools: list[dict],
        handlers: dict[str, Callable[[dict], dict]],
        provider=None,
        model: str | None = None,
        max_tokens: int = 8000,
        max_iterations: int = 12,
    ):
        self.name = name
        self.system = system
        self.tools = tools
        self.handlers = handlers
        self.max_tokens = max_tokens
        self.max_iterations = max_iterations
        if provider is None:
            from daytrader.live.providers import AnthropicProvider
            provider = AnthropicProvider(model or DEFAULT_MODEL)
        self.provider = provider

    @property
    def model(self) -> str:
        return getattr(self.provider, "model", DEFAULT_MODEL)

    def run(self, user_message: str) -> AgentResult:
        return self.provider.run_loop(
            self.system, self.tools, self.handlers, user_message,
            max_tokens=self.max_tokens, max_iterations=self.max_iterations,
        )
