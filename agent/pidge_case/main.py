"""AgentCore Runtime entrypoint for the Pidge case agent. The payload names the scope; the note (or, for kind "chat",
the answer to Grandma's question) comes back typed.

Runs the same shared.case_agent as the Lambda mode, plus AgentCore Memory (actor = the receiver) so the family's
earlier cases are remembered across sessions. Code (pidge-dispatch) still writes the NOTE# row.
"""

import hashlib
import json
import logging
import os

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from pydantic import BaseModel, ValidationError

from shared import case_agent, chat_agent
from shared.enums import AgentInvocationKind, CaseTrigger, ReaderModelChoice

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pidge-case-agent")

CASE_AGENT_MODEL = ReaderModelChoice(os.environ.get("CASE_AGENT_MODEL", ReaderModelChoice.HAIKU_4_5))
MEMORY_ID = os.environ.get("CASE_AGENT_MEMORY_ID", "")  # empty → no AgentCore Memory (local smoke test)
MEMORY_REGION = os.environ.get("AWS_REGION", "us-east-1")
FAMILY_FACTS_NAMESPACE = "/pidge/families/{actorId}/facts"
MINIMUM_SESSION_ID_LENGTH = 33

app = BedrockAgentCoreApp()


class CaseInvocation(BaseModel):
    """What pidge-dispatch sends for a note. Nothing else is read from the payload."""

    receiverUserId: str
    requestId: str
    trigger: CaseTrigger
    caseId: str | None = None


class ChatInvocation(BaseModel):
    """What pidge-dispatch sends for a question: the asker (from her connection row, via code) and the question."""

    kind: AgentInvocationKind
    userId: str
    question: str


def session_id_for(invocation: CaseInvocation) -> str:
    """Deterministic and at least 33 characters: one memory session per request."""
    return f"case-{hashlib.sha256(invocation.requestId.encode()).hexdigest()}"


def build_session_manager(session_id: str, actor_id: str):
    if not MEMORY_ID:
        logger.warning("CASE_AGENT_MEMORY_ID is not set; running without AgentCore Memory")
        return None
    from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig, RetrievalConfig
    from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager

    memory_config = AgentCoreMemoryConfig(
        memory_id=MEMORY_ID,
        session_id=session_id,
        actor_id=actor_id,
        retrieval_config={FAMILY_FACTS_NAMESPACE: RetrievalConfig(top_k=5, relevance_score=0.3)},
    )
    return AgentCoreMemorySessionManager(memory_config, region_name=MEMORY_REGION)


def chat_session_id_for(invocation: ChatInvocation) -> str:
    """The same id pidge-dispatch passes: one chat session per user, at least 33 characters."""
    return f"chat-{hashlib.sha256(invocation.userId.encode()).hexdigest()}"


def answer_question(payload: dict) -> dict:
    """Returns {"reply": ChatReply} or {"error": ...}; same Memory actor as the notes, so Pidge remembers the family."""
    try:
        invocation = ChatInvocation.model_validate(payload)
    except ValidationError as error:
        logger.warning("invalid chat invocation payload: %s", error)
        return {"error": "invalid payload"}
    session_manager = build_session_manager(chat_session_id_for(invocation), invocation.userId)
    agent = chat_agent.build_chat_agent(CASE_AGENT_MODEL, invocation.userId, session_manager=session_manager)
    reply = chat_agent.run_chat_agent(agent, invocation.question)
    logger.info("question answered for %s (%d characters)", invocation.userId, len(reply.reply_text))
    return {"reply": json.loads(reply.model_dump_json())}


@app.entrypoint
def handle_invocation(payload: dict) -> dict:
    """kind "chat" answers a question; anything else is a case note. The runtime turns an uncaught exception into a 500."""
    if payload.get("kind") == AgentInvocationKind.CHAT:
        return answer_question(payload)
    return write_case_note(payload)


def write_case_note(payload: dict) -> dict:
    """Returns {"note": CaseNote} or {"error": ...}."""
    try:
        invocation = CaseInvocation.model_validate(payload)
    except ValidationError as error:
        logger.warning("invalid case invocation payload: %s", error)
        return {"error": "invalid payload"}
    scope = case_agent.CaseScope(invocation.receiverUserId, invocation.requestId, invocation.trigger, invocation.caseId)
    session_manager = build_session_manager(session_id_for(invocation), invocation.receiverUserId)
    agent = case_agent.build_case_agent(CASE_AGENT_MODEL, scope, session_manager=session_manager)
    note = case_agent.run_case_agent(agent, scope)
    logger.info("case note written for %s (%s)", scope.request_id, scope.trigger)
    return {"note": json.loads(note.model_dump_json())}


if __name__ == "__main__":
    app.run()
