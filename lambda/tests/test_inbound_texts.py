"""Inbound WhatsApp, RCS and SMS records parse into ReceivedText: every typed text and media message of a record, a profile
name only from its own wa_id, taps and statuses as nothing, and a sender-less message or a foreign SMS destination raises."""

import json

import pytest

from shared.enums import Channel
from shared.inbound_texts import ReceivedText, parse_rcs_record, parse_sms_record, parse_whatsapp_record
from tests.test_rcs import sns_message as rcs_sns_message
from tests.test_whatsapp import button_reply_tap, sns_message as whatsapp_sns_message

JIMIN_DIGITS = "821011112222"
JIMIN_NUMBER = f"+{JIMIN_DIGITS}"
NEW_PHONE_TEXT = "Mom, new phone, send money"
SENT_AT_EPOCH_SECONDS = "1793059200"  # 2026-10-27T00:00:00Z
SENT_AT = "2026-10-27T00:00:00Z"
PIDGE_SMS_NUMBER = "+18885550199"
SCAMMER_SMS_NUMBER = "+12065550100"
NEW_PHONE_SMS_TEXT = "Mom it's Jimin, new phone"

TEMPLATE_TAP = {"from": JIMIN_DIGITS, "id": "wamid.B", "timestamp": SENT_AT_EPOCH_SECONDS, "type": "button",
                "button": {"payload": "req-1:yes", "text": "Yes"}}
REVERSE_BUTTON_TAP = {**button_reply_tap("reverse:req-1"), "from": JIMIN_DIGITS, "id": "wamid.I"}
REACTION = {"from": JIMIN_DIGITS, "id": "wamid.R", "timestamp": SENT_AT_EPOCH_SECONDS, "type": "reaction",
            "reaction": {"message_id": "wamid.T1", "emoji": "❤"}}
IMAGE = {"from": JIMIN_DIGITS, "id": "wamid.P", "timestamp": SENT_AT_EPOCH_SECONDS, "type": "image",
         "image": {"id": "media-1", "mime_type": "image/jpeg", "caption": "Send money to this account"}}
STATUSES_ONLY = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": "1365540803316739"},
                 "statuses": [{"id": "wamid.X", "status": "delivered", "recipient_id": JIMIN_DIGITS}]}
SMS_RECORD = {"originationNumber": SCAMMER_SMS_NUMBER, "destinationNumber": PIDGE_SMS_NUMBER, "messageKeyword": "KEYWORD_358148451054",
              "messageBody": NEW_PHONE_SMS_TEXT, "inboundMessageId": "sms-1", "previousPublishedMessageId": "out-0"}


def typed_text(message_id: str, body: str, sender_digits: str = JIMIN_DIGITS, timestamp: str = SENT_AT_EPOCH_SECONDS) -> dict:
    return {"from": sender_digits, "id": message_id, "timestamp": timestamp, "type": "text", "text": {"body": body}}


def change_value_with(*messages: dict, contacts: list | None = None) -> dict:
    """One change's value as Meta sends it: the messages, and the contacts[] entries naming their senders when given."""
    change_value = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": "1365540803316739"}, "messages": list(messages)}
    if contacts is not None:
        change_value["contacts"] = contacts
    return change_value


def profile_contact(name: str, sender_digits: str) -> dict:
    return {"profile": {"name": name}, "wa_id": sender_digits}


def message_ids(received_texts: list[ReceivedText]) -> list[str]:
    return [received.provider_message_id for received in received_texts]


def test_whatsapp_text_with_profile_name():
    record = whatsapp_sns_message(change_value_with(typed_text("wamid.T1", NEW_PHONE_TEXT),
                                                    contacts=[profile_contact("Jimin", JIMIN_DIGITS)]))
    assert parse_whatsapp_record(record) == [
        ReceivedText(Channel.WHATSAPP, "wamid.T1", JIMIN_NUMBER, "Jimin", NEW_PHONE_TEXT, SENT_AT)]
    named_for_another_number = whatsapp_sns_message(change_value_with(typed_text("wamid.T2", "hi"),
                                                                      contacts=[profile_contact("Jimin", "447700900123")]))
    assert parse_whatsapp_record(named_for_another_number)[0].sender_profile_name == ""


def test_taps_statuses_and_unknown_types_yield_nothing():
    for change_value in (change_value_with(TEMPLATE_TAP), change_value_with(REVERSE_BUTTON_TAP), STATUSES_ONLY,
                         change_value_with(REACTION)):
        assert parse_whatsapp_record(whatsapp_sns_message(change_value)) == []
    [image] = parse_whatsapp_record(whatsapp_sns_message(change_value_with(IMAGE)))
    assert (image.channel, image.provider_message_id, image.sender_address, image.text) == (
        Channel.WHATSAPP, "wamid.P", JIMIN_NUMBER, None)  # the caption is never read
    two_texts = change_value_with(typed_text("wamid.T1", "first"), typed_text("wamid.T2", "second"))
    assert message_ids(parse_whatsapp_record(whatsapp_sns_message(two_texts))) == ["wamid.T1", "wamid.T2"]
    meta_wrapper = {"whatsAppWebhookEntry": json.dumps({"object": "whatsapp_business_account", "entry": [
        {"id": "1", "changes": [{"value": change_value_with(typed_text("wamid.E1", "a")), "field": "messages"}]},
        {"id": "2", "changes": [{"value": change_value_with(TEMPLATE_TAP, typed_text("wamid.E2", "b")), "field": "messages"}]},
    ]})}
    assert message_ids(parse_whatsapp_record(meta_wrapper)) == ["wamid.E1", "wamid.E2"]


def test_sender_without_from_uses_the_business_scoped_id():
    business_scoped_text = {"from_user_id": "US.13491208655302741918", "id": "wamid.U", "timestamp": SENT_AT_EPOCH_SECONDS,
                            "type": "text", "text": {"body": "hi"}}
    [received] = parse_whatsapp_record(whatsapp_sns_message(change_value_with(
        business_scoped_text, contacts=[{"profile": {"name": "Jimin"}, "user_id": "US.13491208655302741918"}])))
    assert (received.sender_address, received.sender_profile_name) == ("US.13491208655302741918", "")
    sender_less_text = {key: value for key, value in business_scoped_text.items() if key != "from_user_id"}
    with pytest.raises(ValueError):
        parse_whatsapp_record(whatsapp_sns_message(change_value_with(sender_less_text)))
    with pytest.raises(ValueError):
        parse_whatsapp_record(whatsapp_sns_message(change_value_with(typed_text("wamid.F", "hi", sender_digits="82-10"))))


def test_sms_record_parsed_and_foreign_destination_raises():
    assert parse_sms_record(SMS_RECORD, PIDGE_SMS_NUMBER) == [
        ReceivedText(Channel.SMS, "sms-1", SCAMMER_SMS_NUMBER, "", NEW_PHONE_SMS_TEXT, None)]
    with pytest.raises(ValueError):
        parse_sms_record({**SMS_RECORD, "destinationNumber": "+18885550000"}, PIDGE_SMS_NUMBER)


def test_rcs_typed_text_parsed_and_chip_tap_skipped():
    assert parse_rcs_record(rcs_sns_message({"type": "TEXT", "text": "hello"})) == [
        ReceivedText(Channel.RCS, "msg-1", "+821012345678", "", "hello", None)]
    assert parse_rcs_record(rcs_sns_message({"type": "SUGGESTION", "text": "Yes, I sent it", "postbackData": "req-1:yes"})) == []
