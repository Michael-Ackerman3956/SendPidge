"""A request's held row is read under its REQUEST#.inboundChannel: EMAIL# for email (and for rows from before the field),
MESSAGE# for a text channel, and never the other kind's row."""

import pytest

from shared import graph, inbox_rows
from shared.enums import Channel
from tests.test_verdict import aws  # noqa: F401


def put_inbound_row(partition_key: str) -> None:
    graph.get_table().put_item(Item={"PK": partition_key, "SK": graph.METADATA_SORT_KEY, "quarantined": False})


def test_held_row_resolves_by_inbound_channel(aws):  # noqa: F811
    message_id = inbox_rows.message_item_id(Channel.WHATSAPP, "m1")
    sms_message_id = inbox_rows.message_item_id(Channel.SMS, "m1")
    put_inbound_row("EMAIL#inbox/a")
    put_inbound_row(f"MESSAGE#{message_id}")
    put_inbound_row(f"MESSAGE#{sms_message_id}")
    assert (message_id, sms_message_id) == ("whatsapp#m1", "sms#m1")
    assert inbox_rows.get_held_row({"PK": "REQUEST#req-email", "inboundChannel": "email", "emailId": "inbox/a"})["PK"] == "EMAIL#inbox/a"
    assert inbox_rows.get_held_row({"PK": "REQUEST#req-text", "inboundChannel": "whatsapp", "emailId": message_id})["PK"] == (
        "MESSAGE#whatsapp#m1")
    assert inbox_rows.get_held_row({"PK": "REQUEST#req-sms", "inboundChannel": "sms", "emailId": sms_message_id})["PK"] == (
        "MESSAGE#sms#m1")
    assert inbox_rows.get_held_row({"PK": "REQUEST#req-old", "emailId": "inbox/a"})["PK"] == "EMAIL#inbox/a"
    text_request_naming_an_email = {"PK": "REQUEST#req-mixed", "inboundChannel": "whatsapp", "emailId": "inbox/a"}
    assert inbox_rows.get_held_row(text_request_naming_an_email) is None  # a set inboundChannel never falls back to EMAIL#
    with pytest.raises(LookupError):
        inbox_rows.require_held_row(text_request_naming_an_email)
