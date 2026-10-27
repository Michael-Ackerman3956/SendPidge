"""The one runner for every tool-holding agent: each run is capped at AGENT_TURN_LIMIT model turns, and a run that
ends without its typed output raises AgentRunError instead of returning nothing.

The tool-less Reader and reviewer do not use it: with no tools their loop ends after the forced structured-output retry.
"""

from typing import TypeVar

from pydantic import BaseModel
from strands import Agent
from strands.types.exceptions import StructuredOutputException

from shared.agent_enums import AgentRunFailure

AGENT_TURN_LIMIT = 8  # a case run takes at most 5 turns (four tools, then the output tool), a chat run at most 4
TURN_LIMIT_STOP_REASON = "limit_turns"  # Strands 1.62 stops here with no output and raises nothing

AgentOutput = TypeVar("AgentOutput", bound=BaseModel)


class AgentRunError(Exception):
    """A tool-holding agent run produced no output; `failure` says why."""

    def __init__(self, failure: AgentRunFailure) -> None:
        super().__init__(f"agent run failed: {failure}")
        self.failure = failure


def run_agent_once(agent: Agent, prompt: str, output_model: type[AgentOutput]) -> AgentOutput:
    """One capped run → one typed output. Raises AgentRunError on the turn cap or a missing output, never returns None.

    A StructuredOutputException becomes NO_OUTPUT with its chain suppressed: its message quotes the model's invalid
    output, which may hold unsafe text. `from None` keeps that text out of the logged traceback only; on AgentCore,
    opentelemetry-instrument (case-agent.ts entrypoint) still records the original exception in the trace."""
    try:
        result = agent(prompt, structured_output_model=output_model, limits={"turns": AGENT_TURN_LIMIT})
    except StructuredOutputException:
        raise AgentRunError(AgentRunFailure.NO_OUTPUT) from None
    if result.stop_reason == TURN_LIMIT_STOP_REASON:
        raise AgentRunError(AgentRunFailure.TURN_LIMIT)
    if result.structured_output is None:
        raise AgentRunError(AgentRunFailure.NO_OUTPUT)
    return result.structured_output
