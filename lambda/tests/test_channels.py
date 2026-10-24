"""The fan-out: one failing channel never stops the others; every channel failing returns nothing (the WARN follows)."""

import asyncio

from shared import channels, graph
from shared.enums import Channel

REQUEST_ROW = {"PK": "REQUEST#req-1", "SK": "META", "emailId": "inbox/fake.eml", "verdict": {"summary": "Asks for money."}}
CONTACT = graph.Contact("CONTACT#jimin", "Jimin", "jimin", contact_rcs="+821012345678", contact_whatsapp="+821012345678")


def sender_returning(channel: Channel):
    def send(request_row, contact, receiver_name):
        return channel
    send.__name__ = f"ask_on_{channel}"
    return send


def sender_failing(channel: Channel):
    def send(request_row, contact, receiver_name):
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
    whatsapp_only = graph.Contact("CONTACT#jimin", "Jimin", "jimin", contact_whatsapp="+821012345678")
    assert asyncio.run(channels.ask_on_every_channel(REQUEST_ROW, whatsapp_only, "Grandma")) == [Channel.INAPP, Channel.WHATSAPP]
