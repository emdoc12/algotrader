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


class Agent:
    """One LLM agent: a model, a persona, and a toolbox it can act through."""

    def __init__(
        self,
        name: str,
        system: str,
        tools: list[dict],
        handlers: dict[str, Callable[[dict], dict]],
        model: str | None = None,
        max_tokens: int = 8000,
        max_iterations: int = 12,
    ):
        self.name = name
        self.system = system
        self.tools = tools
        self.handlers = handlers
        self.model = model or DEFAULT_MODEL
        self.max_tokens = max_tokens
        self.max_iterations = max_iterations
        self._client = None

    def _client_lazy(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic()
        return self._client

    def run(self, user_message: str) -> AgentResult:
        client = self._client_lazy()
        messages = [{"role": "user", "content": user_message}]
        actions: list[dict] = []

        try:
            for _ in range(self.max_iterations):
                resp = client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=self.system,
                    thinking={"type": "adaptive"},
                    tools=self.tools,
                    messages=messages,
                )

                if resp.stop_reason == "refusal":
                    return AgentResult(text="", actions=actions, refused=True)

                if resp.stop_reason != "tool_use":
                    text = "".join(b.text for b in resp.content if b.type == "text")
                    return AgentResult(text=text, actions=actions)

                # Execute every requested tool, collect results.
                messages.append({"role": "assistant", "content": resp.content})
                tool_results = []
                for block in resp.content:
                    if block.type != "tool_use":
                        continue
                    handler = self.handlers.get(block.name)
                    if handler is None:
                        result = {"ok": False, "error": f"unknown tool {block.name}"}
                    else:
                        try:
                            result = handler(block.input)
                        except Exception as e:  # noqa: BLE001
                            result = {"ok": False, "error": repr(e)}
                    actions.append({"tool": block.name, "input": block.input, "result": result})
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, default=str),
                    })
                messages.append({"role": "user", "content": tool_results})

            return AgentResult(text="(max iterations reached)", actions=actions)
        except Exception as e:  # noqa: BLE001 - network / SDK variability
            return AgentResult(text="", actions=actions, error=repr(e))
