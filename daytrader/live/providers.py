"""Multi-provider LLM abstraction for the trading agents.

The same agent — same system prompt, same tools — can be driven by Claude
(Anthropic), OpenAI, Grok (xAI), or Qwen. Each provider runs the FULL agentic
tool loop locally (we execute the tool handlers and feed results back) and
returns an :class:`AgentResult` identical in shape to the one the existing
``llm_client.Agent`` produces, so downstream code is unchanged.

- ``AnthropicProvider`` uses the official ``anthropic`` SDK with a manual
  tool-use loop, adaptive thinking, and explicit ``refusal`` handling — it
  mirrors ``llm_client.Agent.run`` exactly.
- ``OpenAICompatibleProvider`` uses the official ``openai`` SDK (Chat
  Completions) pointed at a configurable ``base_url``. One class covers OpenAI,
  xAI Grok, and Qwen, since all three expose OpenAI-compatible chat APIs.

Providers construct lazily and degrade gracefully: a missing API key (or any
SDK/network error) is caught and returned as ``AgentResult(error=...)`` — they
never raise out of ``run_loop``.
"""
from __future__ import annotations

import json
import os
from typing import Callable

# Re-export the canonical AgentResult so callers can import it from either
# module. If the existing module is unavailable for any reason, fall back to an
# identical dataclass definition (same fields, same defaults).
try:
    from daytrader.live.llm_client import AgentResult
except Exception:  # pragma: no cover - defensive fallback
    from dataclasses import dataclass, field

    @dataclass
    class AgentResult:  # type: ignore[no-redef]
        text: str
        actions: list[dict] = field(default_factory=list)  # [{tool, input, result}]
        refused: bool = False
        error: str | None = None


Handlers = dict[str, Callable[[dict], dict]]


def _run_handler(handlers: Handlers, name: str, inp: dict) -> dict:
    """Execute a tool handler, mapping unknown tools / exceptions to error dicts."""
    handler = handlers.get(name)
    if handler is None:
        return {"ok": False, "error": f"unknown tool {name}"}
    try:
        return handler(inp)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": repr(e)}


class BaseProvider:
    """A provider runs the full agentic tool loop and returns an AgentResult."""

    name: str = "base"
    model: str = ""

    def run_loop(
        self,
        system: str,
        tools: list[dict],
        handlers: Handlers,
        user_message: str,
        max_tokens: int = 6000,
        max_iterations: int = 12,
    ) -> AgentResult:
        raise NotImplementedError


class AnthropicProvider(BaseProvider):
    """Claude via the official ``anthropic`` SDK (manual tool-use loop).

    Mirrors ``llm_client.Agent.run``: adaptive thinking, ``stop_reason``
    handling for ``refusal``/``tool_use``/``end_turn``, executes each
    requested tool handler, appends ``tool_result`` blocks, and loops.
    """

    name = "claude"

    def __init__(self, model: str = "claude-opus-4-8"):
        self.model = model
        self._client = None

    def _client_lazy(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def run_loop(
        self,
        system: str,
        tools: list[dict],
        handlers: Handlers,
        user_message: str,
        max_tokens: int = 6000,
        max_iterations: int = 12,
    ) -> AgentResult:
        actions: list[dict] = []
        try:
            client = self._client_lazy()
            messages = [{"role": "user", "content": user_message}]

            for _ in range(max_iterations):
                resp = client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    system=system,
                    thinking={"type": "adaptive"},
                    tools=tools,
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
                    result = _run_handler(handlers, block.name, block.input)
                    actions.append({"tool": block.name, "input": block.input, "result": result})
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, default=str),
                    })
                messages.append({"role": "user", "content": tool_results})

            return AgentResult(text="(max iterations reached)", actions=actions)
        except Exception as e:  # noqa: BLE001 - network / SDK / missing-key variability
            return AgentResult(text="", actions=actions, error=repr(e))


def _to_openai_tools(tools: list[dict]) -> list[dict]:
    """Convert Anthropic-style tool schemas to OpenAI function-tools format."""
    converted = []
    for t in tools:
        converted.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        })
    return converted


class OpenAICompatibleProvider(BaseProvider):
    """OpenAI / xAI Grok / Qwen via the official ``openai`` SDK (Chat Completions).

    All three expose OpenAI-compatible chat APIs, so a single class with a
    configurable ``base_url`` + ``api_key_env`` covers them. Runs the
    chat-completions tool loop manually so we execute handlers locally and
    record actions identically to the Anthropic path.
    """

    def __init__(self, name: str, model: str, base_url: str, api_key_env: str):
        self.name = name
        self.model = model
        self.base_url = base_url
        self.api_key_env = api_key_env
        self._client = None

    def _client_lazy(self):
        if self._client is None:
            from openai import OpenAI

            # KeyError here (missing/empty key) is caught by run_loop and
            # surfaced as AgentResult(error=...), never raised to the caller.
            self._client = OpenAI(
                base_url=self.base_url,
                api_key=os.environ[self.api_key_env],
            )
        return self._client

    def run_loop(
        self,
        system: str,
        tools: list[dict],
        handlers: Handlers,
        user_message: str,
        max_tokens: int = 6000,
        max_iterations: int = 12,
    ) -> AgentResult:
        actions: list[dict] = []
        try:
            client = self._client_lazy()
            oai_tools = _to_openai_tools(tools)
            messages: list[dict] = [
                {"role": "system", "content": system},
                {"role": "user", "content": user_message},
            ]

            for _ in range(max_iterations):
                resp = client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=oai_tools,
                    tool_choice="auto",
                    max_tokens=max_tokens,
                )

                msg = resp.choices[0].message
                tool_calls = getattr(msg, "tool_calls", None)

                if not tool_calls:
                    return AgentResult(text=msg.content or "", actions=actions)

                # Append the assistant message (must carry the tool_calls), then
                # one tool-role message per executed call.
                messages.append({
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in tool_calls
                    ],
                })

                for tc in tool_calls:
                    name = tc.function.name
                    try:
                        inp = json.loads(tc.function.arguments or "{}")
                    except (ValueError, TypeError):
                        inp = {}
                    result = _run_handler(handlers, name, inp)
                    actions.append({"tool": name, "input": inp, "result": result})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, default=str),
                    })

            return AgentResult(text="(max iterations reached)", actions=actions)
        except Exception as e:  # noqa: BLE001 - network / SDK / missing-key variability
            return AgentResult(text="", actions=actions, error=repr(e))


def make_provider(spec: dict) -> BaseProvider:
    """Build a provider from a spec dict.

    spec = {"provider": "anthropic"|"openai_compatible", "name", "model",
            "base_url", "api_key_env"}
    """
    kind = spec.get("provider")
    if kind == "anthropic":
        return AnthropicProvider(model=spec.get("model", "claude-opus-4-8"))
    if kind == "openai_compatible":
        return OpenAICompatibleProvider(
            name=spec["name"],
            model=spec["model"],
            base_url=spec["base_url"],
            api_key_env=spec["api_key_env"],
        )
    raise ValueError(f"unknown provider kind: {kind!r}")


def default_team_providers() -> dict[str, BaseProvider]:
    """The four contestants, fully overridable by environment variables."""
    return {
        "claude": AnthropicProvider(
            model=os.environ.get("CLAUDE_MODEL", "claude-opus-4-8"),
        ),
        "openai": OpenAICompatibleProvider(
            name="openai",
            model=os.environ.get("OPENAI_MODEL", "gpt-5.1"),
            base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            api_key_env="OPENAI_API_KEY",
        ),
        "grok": OpenAICompatibleProvider(
            name="grok",
            model=os.environ.get("XAI_MODEL", "grok-4"),
            base_url=os.environ.get("XAI_BASE_URL", "https://api.x.ai/v1"),
            api_key_env="XAI_API_KEY",
        ),
        "qwen": OpenAICompatibleProvider(
            name="qwen",
            model=os.environ.get("QWEN_MODEL", "qwen3.6"),
            base_url=os.environ.get(
                "QWEN_BASE_URL",
                "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            ),
            api_key_env="DASHSCOPE_API_KEY",
        ),
    }


# Which env var must be set for each team's provider to have credentials.
_TEAM_KEY_ENV = {
    "claude": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "grok": "XAI_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
}


def has_key(provider) -> bool:
    """True if the API key env var for this team/provider is set and non-empty.

    Accepts either a team name (str) or a provider instance. For
    OpenAI-compatible providers the instance carries its own ``api_key_env``;
    for the Anthropic provider we map by team name.
    """
    if isinstance(provider, OpenAICompatibleProvider):
        env = provider.api_key_env
    elif isinstance(provider, str):
        env = _TEAM_KEY_ENV.get(provider)
    elif isinstance(provider, AnthropicProvider):
        env = "ANTHROPIC_API_KEY"
    else:
        env = getattr(provider, "api_key_env", None)
    if not env:
        return False
    return bool(os.environ.get(env, "").strip())
