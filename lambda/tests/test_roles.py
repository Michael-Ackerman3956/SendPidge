"""The role a PWA user gets comes from their own USER#<id>/PROFILE row, never from the stack's environment: a decoy
RECEIVER_USER_ID changes nothing, and no profile, an unknown role or an org admin is not allowed."""

import pytest

from shared import graph
from tests.helpers.profiles import seed_contact_roles, seed_resident
from tests.test_verdict import aws  # noqa: F401
from tests.test_verify_contact import seed_circle_contact, send_action, verdict_handler_without_profiles  # noqa: F401

BOB_ADDRESS = "bob@example.com"


@pytest.fixture
def decoy_environment(monkeypatch):
    """The old single-receiver variables, pointing at a user who has no profile."""
    monkeypatch.setenv("RECEIVER_USER_ID", "grandma")
    monkeypatch.setenv("RECEIVER_NAME", "Grandma")
    monkeypatch.setenv("RECEIVER_ADDRESS", "grandma@example.com")


def put_profile(user_id: str, fields: dict) -> None:
    graph.get_table().put_item(Item={"PK": f"USER#{user_id}", "SK": "PROFILE", "tokenHash": f"{user_id}-token-hash", **fields})


def test_role_from_profile_row_not_context(decoy_environment, verdict_handler_without_profiles):  # noqa: F811
    handler, pushed_messages = verdict_handler_without_profiles
    seed_resident(BOB_ADDRESS, "bob", "Bob")
    seed_circle_contact(BOB_ADDRESS, "jimin")
    put_profile("jimin", {})
    assert seed_contact_roles(BOB_ADDRESS) == []
    put_profile("eve", {"tokenHash": "x", "role": "superuser"})
    put_profile("admin", {"role": "org_admin"})

    send_action(handler, "bob", {"action": "sync"})
    assert (pushed_messages[-1]["type"], pushed_messages[-1]["role"], pushed_messages[-1]["receiverName"]) == ("sync_state", "receiver", "Bob")
    send_action(handler, "jimin", {"action": "sync"})
    assert (pushed_messages[-1]["type"], pushed_messages[-1]["role"]) == ("sync_state", "contact")

    not_allowed = {"type": "action_rejected", "requestId": None, "reason": "not_allowed"}
    pushed_messages.clear()
    send_action(handler, "grandma", {"action": "sync"})
    send_action(handler, "grandma", {"action": "ask_pidge", "text": "Was that Jimin?"})
    send_action(handler, "eve", {"action": "sync"})
    send_action(handler, "admin", {"action": "sync"})
    assert pushed_messages == [not_allowed] * 4
