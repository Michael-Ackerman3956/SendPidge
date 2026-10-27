"""The fan-out: one failing channel never stops the others; every channel failing returns nothing (the WARN follows)."""

import asyncio
import json
from decimal import Decimal

import pytest

from shared import channels, graph, reach_points
from shared.enums import Channel, VerifiedVia

REQUEST_ROW = {"PK": "REQUEST#req-1", "SK": "META", "emailId": "inbox/fake.eml", "verdict": {"summary": "Asks for money."}}
CONTACT = graph.Contact("CONTACT#jimin", "Jimin", "jimin", (
    reach_points.ReachPoint(Channel.RCS, "+821012345678", VerifiedVia.SEED), reach_points.ReachPoint(Channel.WHATSAPP, "+821012345678", VerifiedVia.SEED),
))


def sender_returning(channel: Channel):
    def send(request_row, contact, receiver_name, destination):
        return channel
    send.__name__ = f"ask_on_{channel}"
    return send


def sender_failing(channel: Channel):
    def send(request_row, contact, receiver_name, destination):
        raise RuntimeError(f"{channel} is down")
    send.__name__ = f"ask_on_{channel}"
    return send


def test_one_failing_channel_leaves_the_others_sent(monkeypatch):
    monkeypatch.setattr(channels, "RCS_ENABLED", True)
    monkeypatch.setattr(channels, "WHATSAPP_ENABLED", True)
    monkeypatch.setattr(channels, "ask_in_app", sender_returning(Channel.INAPP))
    monkeypatch.setattr(channels, "ask_on_rcs", sender_failing(Channel.RCS))
    monkeypatch.setattr(channels, "ask_on_whatsapp", sender_returning(Channel.WHATSAPP))
    sent = asyncio.run(channels.ask_on_every_channel(REQUEST_ROW, CONTACT, "Grandma"))
    assert sent == [Channel.INAPP, Channel.WHATSAPP]


def test_every_channel_failing_returns_nothing(monkeypatch):
    monkeypatch.setattr(channels, "RCS_ENABLED", True)
    monkeypatch.setattr(channels, "WHATSAPP_ENABLED", True)
    monkeypatch.setattr(channels, "ask_in_app", sender_failing(Channel.INAPP))
    monkeypatch.setattr(channels, "ask_on_rcs", sender_failing(Channel.RCS))
    monkeypatch.setattr(channels, "ask_on_whatsapp", sender_failing(Channel.WHATSAPP))
    assert asyncio.run(channels.ask_on_every_channel(REQUEST_ROW, CONTACT, "Grandma")) == []


def test_a_channel_without_a_contact_address_is_skipped(monkeypatch):
    monkeypatch.setattr(channels, "RCS_ENABLED", True)
    monkeypatch.setattr(channels, "WHATSAPP_ENABLED", True)
    monkeypatch.setattr(channels, "ask_in_app", sender_returning(Channel.INAPP))
    monkeypatch.setattr(channels, "ask_on_rcs", sender_failing(Channel.RCS))
    monkeypatch.setattr(channels, "ask_on_whatsapp", sender_returning(Channel.WHATSAPP))
    whatsapp_only = graph.Contact("CONTACT#jimin", "Jimin", "jimin", (reach_points.ReachPoint(Channel.WHATSAPP, "+821012345678", VerifiedVia.SEED),))
    assert asyncio.run(channels.ask_on_every_channel(REQUEST_ROW, whatsapp_only, "Grandma")) == [Channel.INAPP, Channel.WHATSAPP]


def test_push_encoding_turns_dynamodb_decimals_into_json_numbers():
    message = {"recoveryPack": {"steps": [{"order": Decimal("1"), "deadlineDays": Decimal("60")}]}, "confidence": Decimal("0.85")}
    assert json.loads(channels.encode_push_message(message)) == {
        "recoveryPack": {"steps": [{"order": 1, "deadlineDays": 60}]}, "confidence": 0.85}


def test_push_encoding_still_rejects_other_unknown_types():
    with pytest.raises(TypeError):
        channels.encode_push_message({"when": object()})
