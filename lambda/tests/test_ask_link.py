"""The signed 'No' link: GET shows the button and writes nothing; POST denies once through record_answer; the same link
again is 'already recorded'; a tampered, expired, malformed or unknown link records nothing."""

import importlib
import time

import pytest

from shared import ask_link, graph
from shared.enums import AskLinkOutcome, Channel, RequestState
from tests.helpers.profiles import seed_resident
from tests.test_verdict import RECEIVER_ADDRESS, RECEIVER_USER_ID, aws, new_request, status_of  # noqa: F401

BASE_URL = "https://app.sendpidge.example"
REQUEST_ID = "req-0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
NOW = 1_800_000_000


@pytest.fixture
def signing_key(monkeypatch):
    monkeypatch.setattr(ask_link, "_signing_key", b"test-signing-key")


@pytest.fixture
def link_handler(aws, signing_key, monkeypatch):  # noqa: F811
    seed_resident(RECEIVER_ADDRESS, RECEIVER_USER_ID, "Grandma")
    monkeypatch.setenv("RECEIPT_SENDER", "pidge@sendpidge.example")
    monkeypatch.setenv("WEBSOCKET_ENDPOINT", "https://example.com")
    handler = importlib.import_module("pidge_ask_link.handler")
    return importlib.reload(handler)


def split_link(link: str) -> tuple[str, dict]:
    path, query = link.removeprefix(BASE_URL).split("?")
    return path, dict(field.split("=") for field in query.split("&"))


def request_for(link: str, method: str) -> dict:
    path, query = split_link(link)
    return {"requestContext": {"http": {"method": method}}, "rawPath": path, "queryStringParameters": query}


def test_a_built_link_verifies_and_every_tampering_is_named(signing_key):
    link = ask_link.build_no_link(BASE_URL, REQUEST_ID, NOW)
    path, query = split_link(link)
    assert path == f"/no/{REQUEST_ID}" and int(query["exp"]) == NOW + ask_link.LINK_LIFETIME_SECONDS
    check = ask_link.verify_no_link(path, query, NOW)
    assert (check.outcome, check.request_id, check.signature) == (AskLinkOutcome.VALID, REQUEST_ID, query["sig"])
    assert ask_link.verify_no_link(path, query, NOW + ask_link.LINK_LIFETIME_SECONDS + 1).outcome is AskLinkOutcome.EXPIRED
    assert ask_link.verify_no_link(path, {**query, "sig": "0" * 64}, NOW).outcome is AskLinkOutcome.BAD_SIGNATURE
    assert ask_link.verify_no_link(path, {**query, "exp": str(int(query["exp"]) + 1)}, NOW).outcome is AskLinkOutcome.BAD_SIGNATURE
    other_request = f"/no/{REQUEST_ID[:-1]}c"
    assert ask_link.verify_no_link(other_request, query, NOW).outcome is AskLinkOutcome.BAD_SIGNATURE
    for path_variant, query_variant in [("/no/req-1", query), ("/yes/" + REQUEST_ID, query), (path, {**query, "sig": "abc"}),
                                        (path, {"sig": query["sig"]}), (path, None)]:
        assert ask_link.verify_no_link(path_variant, query_variant, NOW).outcome is AskLinkOutcome.MALFORMED


def test_get_shows_the_button_and_writes_nothing_then_post_denies_once(link_handler):
    request_id = new_request()
    link = ask_link.build_no_link(BASE_URL, request_id, int(time.time()))
    shown = link_handler.handle(request_for(link, "GET"), None)
    assert shown["statusCode"] == 200 and 'method="post"' in shown["body"] and "No, that wasn" in shown["body"]
    assert status_of(request_id) == str(RequestState.PENDING)
    recorded = link_handler.handle(request_for(link, "POST"), None)
    assert recorded["statusCode"] == 200 and "Recorded." in recorded["body"]
    request_row = graph.get_request(request_id)
    assert (request_row["status"], request_row["repliedVia"]) == (str(RequestState.DENIED), str(Channel.EMAIL))
    assert request_row["inboundMessageIds"] == [f"link:no:{split_link(link)[1]['sig'][:16]}"]
    again = link_handler.handle(request_for(link, "POST"), None)
    assert again["statusCode"] == 200 and "Already recorded." in again["body"]
    assert graph.get_request(request_id)["inboundMessageIds"] == request_row["inboundMessageIds"]


def test_a_bad_expired_or_unknown_link_records_nothing(link_handler):
    request_id = new_request()
    link = ask_link.build_no_link(BASE_URL, request_id, int(time.time()))
    path, query = split_link(link)
    tampered = {"requestContext": {"http": {"method": "POST"}}, "rawPath": path, "queryStringParameters": {**query, "sig": "0" * 64}}
    assert link_handler.handle(tampered, None)["statusCode"] == 404
    expired_link = ask_link.build_no_link(BASE_URL, request_id, int(time.time()) - ask_link.LINK_LIFETIME_SECONDS - 60)
    assert link_handler.handle(request_for(expired_link, "POST"), None)["statusCode"] == 410
    unknown_link = ask_link.build_no_link(BASE_URL, REQUEST_ID, int(time.time()))
    assert link_handler.handle(request_for(unknown_link, "POST"), None)["statusCode"] == 404
    assert link_handler.handle(request_for(link, "DELETE"), None)["statusCode"] == 405
    assert status_of(request_id) == str(RequestState.PENDING)
