"""The tool-calling loop, shared by both architectures.

Both architectures use the same loop and the same tools, so when their results
differ it is the orchestration that caused it, not a difference in capability.
"""
from __future__ import annotations

import json

from src.config import MAX_TOOL_STEPS
from src.llm import LlmCounter, chat
from src.tools import TOOL_SCHEMAS, Toolbox

MAX_TOOL_RESULT_CHARS = 3000  # trim long tool results so they don't eat the whole context window


class ToolLoopRunner:
    """Wraps the tool-calling loop as a small stateful object instead of one
    long function, so the per-step bookkeeping (messages, toolbox, counter)
    reads as attributes rather than parameters threaded through helpers.
    """

    def __init__(self, messages: list[dict], toolbox: Toolbox, counter: LlmCounter, max_steps: int = MAX_TOOL_STEPS):
        self.messages = messages
        self.toolbox = toolbox
        self.counter = counter
        self.max_steps = max_steps

    def run(self) -> str:
        for _ in range(self.max_steps):
            # the closing summary isn't used downstream - keeping it short saves most of the latency
            message = chat(self.messages, self.counter, tools=TOOL_SCHEMAS, max_tokens=400)
            tool_calls = message.tool_calls or []
            self._record_assistant_turn(message, tool_calls)

            if not tool_calls:
                return message.content or ""

            for call in tool_calls:
                self._run_one_call(call)

        return ""

    def _record_assistant_turn(self, message, tool_calls) -> None:
        if tool_calls:
            self.messages.append({
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
                    for c in tool_calls
                ],
            })
        else:
            self.messages.append({"role": "assistant", "content": message.content or ""})

    def _run_one_call(self, call) -> None:
        try:
            arguments = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError:
            arguments = {}
        result = self.toolbox.run(call.function.name, arguments)
        self.messages.append({
            "role": "tool",
            "tool_call_id": call.id,
            "content": json.dumps(result, default=str)[:MAX_TOOL_RESULT_CHARS],
        })


def run_tool_loop(messages: list[dict], toolbox: Toolbox, counter: LlmCounter, max_steps: int = MAX_TOOL_STEPS) -> str:
    """Let the model call tools until it stops asking. Returns its closing text."""
    return ToolLoopRunner(messages, toolbox, counter, max_steps).run()
