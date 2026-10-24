"""Ask the Pidge chat agent one question in-process (lambda mode), against the deployed table, without writing rows.

Usage: python scripts/ask_pidge.py --question "Was that Jimin?" [--user-id grandma] [--fake-guardrail pass|hit|down]
Without --fake-guardrail the real guardrail is called (CHAT_GUARDRAIL_ID / CHAT_GUARDRAIL_VERSION must be set).
Needs AWS_PROFILE (or ambient credentials), TABLE_NAME and CASE_AGENT_MODEL in the environment.
"""

import argparse
import os
import sys
from enum import StrEnum
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambda"))

from shared import chat, chat_agent  # noqa: E402
from shared.enums import GuardrailOutcome, ReaderModelChoice  # noqa: E402


class FakeGuardrail(StrEnum):
    PASS = "pass"
    HIT = "hit"
    DOWN = "down"


def check_question(question: str, fake_guardrail: FakeGuardrail | None) -> GuardrailOutcome:
    if fake_guardrail is FakeGuardrail.DOWN:
        raise ConnectionError("fake guardrail is down")
    if fake_guardrail is FakeGuardrail.HIT:
        return GuardrailOutcome.INTERVENED
    if fake_guardrail is FakeGuardrail.PASS:
        return GuardrailOutcome.PASSED
    return chat.check_prompt_attack(question)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--question", required=True)
    parser.add_argument("--user-id", default="grandma")
    parser.add_argument("--fake-guardrail", type=FakeGuardrail, choices=list(FakeGuardrail), default=None)
    arguments = parser.parse_args()
    model_choice = ReaderModelChoice(os.environ.get("CASE_AGENT_MODEL", ReaderModelChoice.OPUS_4_5))
    try:
        guardrail_outcome = check_question(arguments.question, arguments.fake_guardrail)
    except Exception as error:
        print(f"refusal (guardrail unavailable: {error}): {chat.UNAVAILABLE_REFUSAL}")
        return
    if guardrail_outcome is GuardrailOutcome.INTERVENED:
        print(f"refusal (guardrailHit): {chat.PROMPT_ATTACK_REFUSAL}")
        return
    agent = chat_agent.build_chat_agent(model_choice, arguments.user_id)
    reply = chat_agent.run_chat_agent(agent, arguments.question)
    tool_names = [name for name, metrics in agent.event_loop_metrics.tool_metrics.items() if metrics.call_count]
    print(f"reply: {reply.reply_text}")
    print(f"tools called: {tool_names or 'none'}")


if __name__ == "__main__":
    main()
