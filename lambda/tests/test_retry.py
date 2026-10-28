"""The re-ask timer: an unanswered ask is re-sent once with a fresh window, the second silence is timeout, an answer stops it."""

import importlib

import pytest

from shared import graph, verdict
from shared.enums import RequestState, VerdictAnswer
from tests.helpers.profiles import seed_resident
from tests.test_verdict import JIMIN, RECEIVER_ADDRESS, RECEIVER_USER_ID, aws, at, new_request  # noqa: F401


@pytest.fixture
def retry_handler(aws, monkeypatch):  # noqa: F811
    seed_resident(RECEIVER_ADDRESS, RECEIVER_USER_ID, "Grandma")
    monkeypatch.setenv("RE_ASK_DELAY_SECONDS", "600")
    monkeypatch.setenv("VOICE_ENABLED", "false")
    monkeypatch.setenv("WEBSOCKET_ENDPOINT", "https://example.com")
    handler = importlib.import_module("pidge_retry.handler")
    importlib.reload(handler)
    sent, scheduled, pushed = [], [], []
    monkeypatch.setattr(handler.channels, "ask_on_every_channel", lambda request_row, contact, name: fake_ask(sent, request_row))
    monkeypatch.setattr(handler.schedules, "schedule_one_time_invocation", lambda name, fire_at, arn, payload: scheduled.append((name, payload)))
    monkeypatch.setattr(handler.channels, "push_to_user", lambda user_id, message: pushed.append((user_id, message["request"]["status"])) or 1)
    return handler, sent, scheduled, pushed


class FakeContext:
    invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:retry"


async def fake_ask(sent, request_row):
    sent.append(request_row["PK"])
    return []


def test_first_timer_re_asks_once_and_books_the_last_timer(aws, retry_handler):
    handler, sent, scheduled, pushed = retry_handler
    request_id = new_request()
    old_expiry = graph.get_request(request_id)["expiresAt"]
    handler.handle({"requestId": request_id, "attempt": 1}, FakeContext())
    refreshed = graph.get_request(request_id)
    assert refreshed["status"] == "pending" and refreshed["expiresAt"] >= old_expiry and refreshed["reAskedAt"]
    assert sent == [f"REQUEST#{request_id}"]
    assert scheduled == [(f"retry-{request_id}-2", {"requestId": request_id, "attempt": 2})]
    assert pushed == []


def test_second_timer_marks_timeout_and_tells_both_sides(aws, retry_handler):
    handler, sent, scheduled, pushed = retry_handler
    request_id = new_request()
    handler.handle({"requestId": request_id, "attempt": 2}, FakeContext())
    assert graph.get_request(request_id)["status"] == RequestState.TIMEOUT
    assert sent == [] and scheduled == []
    assert sorted(pushed) == [("grandma", RequestState.TIMEOUT), ("jimin", RequestState.TIMEOUT)]


def test_an_answered_request_is_left_alone(aws, retry_handler):
    handler, sent, scheduled, pushed = retry_handler
    request_id = new_request()
    verdict.record_answer(request_id, VerdictAnswer.NO, JIMIN, "m1", at(0))
    handler.handle({"requestId": request_id, "attempt": 1}, FakeContext())
    handler.handle({"requestId": request_id, "attempt": 2}, FakeContext())
    assert graph.get_request(request_id)["status"] == "denied" and sent == [] and scheduled == [] and pushed == []
