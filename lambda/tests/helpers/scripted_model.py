"""A Strands `Model` double that answers from a script, so tests run the real Agent loop (structured-output tool,
forced retry, validation) without Bedrock. A prompt with no script raises; there is never a default verdict."""

import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel
from strands.models import Model
from strands.tools.structured_output.structured_output_tool import StructuredOutputTool
from strands.types.content import Messages
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolChoice, ToolSpec


class UnscriptedPromptError(Exception):
    """The model was called with a prompt no script matches, or the matching script has no step left."""


class ScriptedStepKind(Enum):
    VERDICT_TOOL = "verdict_tool"  # call the structured-output tool with the scripted fields
    TEXT_REPLY = "text_reply"      # answer in plain text and call no tool


@dataclass(frozen=True)
class ScriptedStep:
    kind: ScriptedStepKind
    verdict_fields: dict | None = None
    reply_text: str | None = None


def verdict_step(verdict_fields: dict) -> ScriptedStep:
    return ScriptedStep(ScriptedStepKind.VERDICT_TOOL, verdict_fields=verdict_fields)


def text_reply_step(reply_text: str) -> ScriptedStep:
    return ScriptedStep(ScriptedStepKind.TEXT_REPLY, reply_text=reply_text)


@dataclass(frozen=True)
class RecordedModelCall:
    prompt_text: str
    matched_prompt_fragment: str
    offered_tool_names: tuple[str, ...]
    is_forced_retry: bool  # Strands set tool_choice: the model already answered once without the tool
    step: ScriptedStep
    user_text_count: int  # user text blocks the model saw: 1 for one email read alone, 2 on its forced retry


def list_user_texts(messages: Messages) -> list[str]:
    return [
        content_block["text"]
        for message in messages if message["role"] == "user"
        for content_block in message["content"] if "text" in content_block
    ]


def latest_prompt_text(messages: Messages, is_forced_retry: bool) -> str:
    """The newest user text, skipping Strands' own retry instruction when the retry is forced; "" when there is none."""
    user_texts = list_user_texts(messages)
    prompt_position = -2 if is_forced_retry else -1
    if len(user_texts) < -prompt_position:
        return ""  # matches no script fragment, so next_step_for records it and raises
    return user_texts[prompt_position]


class ScriptedModel(Model):
    """`scripts` maps a prompt fragment (e.g. an email subject) to the ordered steps that prompt's calls consume.

    Production turns any model exception into a ReaderFailure or EscalationFailure, so every raise is also kept in
    raised_errors; a test asserts it is empty."""

    _supports_structured_output = True  # as BedrockModel: the forced retry takes the production branch

    def __init__(self, structured_output_model: type[BaseModel], scripts: dict[str, list[ScriptedStep]]) -> None:
        self.structured_output_tool_name = StructuredOutputTool(structured_output_model).tool_name
        self.remaining_steps_by_fragment = {fragment: list(steps) for fragment, steps in scripts.items()}
        self.recorded_calls: list[RecordedModelCall] = []
        self.raised_errors: list[str] = []
        self.model_config: dict[str, Any] = {}

    def update_config(self, **model_config: Any) -> None:
        self.model_config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        return self.model_config

    def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError("the production agents use structured_output_model= through the Agent loop, never this path")

    def refuse(self, message: str) -> None:
        self.raised_errors.append(message)
        raise UnscriptedPromptError(message)

    def next_step_for(self, prompt_text: str) -> tuple[str, ScriptedStep]:
        matched_fragments = [fragment for fragment in self.remaining_steps_by_fragment if fragment in prompt_text]
        if len(matched_fragments) != 1:
            self.refuse(f"{len(matched_fragments)} script(s) match the prompt {prompt_text[:80]!r}: {matched_fragments}")
        matched_fragment = matched_fragments[0]
        remaining_steps = self.remaining_steps_by_fragment[matched_fragment]
        if not remaining_steps:
            self.refuse(f"the script for {matched_fragment!r} has no step left")
        return matched_fragment, remaining_steps.pop(0)

    async def stream(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        *,
        tool_choice: ToolChoice | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[StreamEvent, None]:
        is_forced_retry = tool_choice is not None
        prompt_text = latest_prompt_text(messages, is_forced_retry)
        matched_fragment, step = self.next_step_for(prompt_text)
        offered_tool_names = tuple(tool_spec["name"] for tool_spec in tool_specs) if tool_specs else ()
        self.recorded_calls.append(RecordedModelCall(prompt_text, matched_fragment, offered_tool_names, is_forced_retry, step,
                                                     len(list_user_texts(messages))))
        yield {"messageStart": {"role": "assistant"}}
        if step.kind is ScriptedStepKind.TEXT_REPLY:
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": step.reply_text}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
            return
        if self.structured_output_tool_name not in offered_tool_names:
            self.refuse(f"{self.structured_output_tool_name} was not offered; tools were {offered_tool_names}")
        tool_use_id = f"scripted-{len(self.recorded_calls)}"
        yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": tool_use_id, "name": self.structured_output_tool_name}}}}
        yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(step.verdict_fields)}}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}
