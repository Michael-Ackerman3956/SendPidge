"""A resident's send through the production pidge_send handler on moto, with recording provider clients and holds and windows
made by the production pidge_inbound pipeline: only to an address Pidge may ask of her own circle, from her own address,
refused while the conversation has a pending hold or Meta's window is shut, 30 an hour (a counter failure refuses), phone
channels only for the messenger resident, and every refusal writes nothing and costs no slot."""

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest
from botocore.exceptions import ClientError

from pidge_send import handler as send_handler
from shared import channels, circle, graph, hourly_counters, verdict
from shared.enums import ActionOutcome, Channel, Relationship, VerdictAnswer
from tests.helpers.profiles import seed_resident
from tests.helpers.scripted_model import verdict_step
from tests.test_inbound import (
    GRANDMA_ADDRESS, HELLO_TEXT, JIMINS_PENDING_DIGITS, NO_ASK_FROM_JIMIN, SMS_TOPIC_ARN, WAITING_NUMBER, WHATSAPP_TOPIC_ARN,
    LoadedInboundHandler, load_inbound_handler, message_row, seed_grandma_and_jimin, whatsapp_text_record,
)
from tests.test_inbound_texts import JIMIN_DIGITS, JIMIN_NUMBER, NEW_PHONE_SMS_TEXT, NEW_PHONE_TEXT, SMS_RECORD
from tests.test_reader_pipeline import JIMIN_MONEY_ASK
from tests.test_reader_receiver import rows_with_partition_prefix
from tests.test_roles import put_profile
from tests.test_verdict import aws  # noqa: F401
from tests.test_verify_contact import send_action

GRANDMA_USER_ID = "grandma"
BOB_ADDRESS = "bob@sendpidge.example"
RECEIPT_SENDER = "pidge@mail.sendpidge.example"  # its domain is the sending domain; Grandma's address is on the receiving one
SMS_PHONE_NUMBER_ARN = "arn:aws:sms-voice:us-east-1:123456789012:phone-number/phone-test"
RCS_AGENT_ARN = "arn:aws:sms-voice:us-east-1:123456789012:rcs-agent/agent-test"
WHATSAPP_PHONE_NUMBER_ID = "phone-number-id-test"
FROZEN_HOUR_LABEL = "2026-10-28T09"
REPLY_TEXT = "See you on Sunday"
TYPED_PHONE = "+821077778888"  # on a row, saved from the form's phone field: never checked
SUMI_NUMBER = "+447700900555"
SECONDS_PER_HOUR = 3600


@dataclass
class RecordingProviders:
    """SES, EUM Social and EUM SMS/RCS in one: every call recorded as (operation, arguments); when failing, it raises after."""

    calls: list[tuple[str, dict]] = field(default_factory=list)
    is_failing: bool = False

    def record(self, operation_name: str, arguments: dict, response: dict) -> dict:
        self.calls.append((operation_name, arguments))
        if self.is_failing:
            raise ClientError({"Error": {"Code": "ServiceUnavailableException", "Message": "injected"}}, operation_name)
        return response

    def send_email(self, **arguments):
        return self.record("SendEmail", arguments, {"MessageId": "ses-1"})

    def send_whatsapp_message(self, **arguments):
        return self.record("SendWhatsAppMessage", arguments, {"messageId": "wamid.S1"})

    def send_text_message(self, **arguments):
        return self.record("SendTextMessage", arguments, {"MessageId": "text-1"})


@dataclass
class LoadedSendHandler:
    inbound: LoadedInboundHandler
    providers: RecordingProviders = field(default_factory=RecordingProviders)
    user_pushes: list[tuple[str, dict, bool]] = field(default_factory=list)  # (user id, wire JSON, should_remove_gone_connections)
    socket_replies: list[dict] = field(default_factory=list)  # wire JSON answered on the sending socket

    def record_user_push(self, user_id: str, message: dict, should_remove_gone_connections: bool = True) -> int:
        self.user_pushes.append((user_id, json.loads(channels.encode_push_message(message)), should_remove_gone_connections))
        return 1

    def record_socket_reply(self, _connection_id: str, message: dict) -> None:
        self.socket_replies.append(json.loads(channels.encode_push_message(message)))

    def send(self, user_id: str, channel: Channel, to: str, text: str = REPLY_TEXT, **extra_fields) -> None:
        send_action(send_handler, user_id, {"action": "send_message", "channel": str(channel), "to": to, "text": text, **extra_fields})

    def rejection_reasons(self) -> list[str]:
        return [reply["reason"] for reply in self.socket_replies if reply["type"] == "action_rejected"]

    def message_state_pushes(self, user_id: str) -> list[tuple[dict, bool]]:
        return [(message["message"], should_remove) for pushed_user_id, message, should_remove in self.user_pushes
                if pushed_user_id == user_id and message["type"] == "message_state"]


@pytest.fixture
def sender(aws, monkeypatch):  # noqa: F811
    """pidge_send as deployed (Grandma the messenger resident, WhatsApp on, RCS and SMS off), the inbound pipeline loaded
    for holds and windows, the providers and every push recorded, and the hour frozen."""
    environment = {"MESSENGER_RESIDENT_ADDRESS": GRANDMA_ADDRESS, "WHATSAPP_ENABLED": "true", "RCS_ENABLED": "false",
                   "SMS_ENABLED": "false", "RECEIPT_SENDER": RECEIPT_SENDER,
                   "SMS_PHONE_NUMBER_ARN": SMS_PHONE_NUMBER_ARN, "RCS_AGENT_ARN": RCS_AGENT_ARN,
                   "WHATSAPP_PHONE_NUMBER_ID": WHATSAPP_PHONE_NUMBER_ID, "WHATSAPP_META_API_VERSION": "v22.0"}
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    loaded = LoadedSendHandler(load_inbound_handler(monkeypatch))
    for client_getter_name in ("get_ses_client", "get_whatsapp_client", "get_rcs_client"):
        monkeypatch.setattr(channels, client_getter_name, lambda: loaded.providers)
    monkeypatch.setattr(channels, "push_to_user", loaded.record_user_push)
    monkeypatch.setattr(channels, "push_to_connection", loaded.record_socket_reply)
    monkeypatch.setattr(hourly_counters, "current_hour_label", lambda: FROZEN_HOUR_LABEL)
    seed_grandma_and_jimin()
    return loaded


def sent_seconds_ago(seconds: int) -> str:
    return str(int(time.time()) - seconds)


def receive_whatsapp_from_jimin(sender: LoadedSendHandler, message_id: str, text: str, seconds_ago: int) -> None:
    sender.inbound.handle(WHATSAPP_TOPIC_ARN, whatsapp_text_record(message_id, text, JIMIN_DIGITS, "Jimin", sent_seconds_ago(seconds_ago)))


def out_rows() -> list[dict]:
    return [row for row in rows_with_partition_prefix("MESSAGE#") if row["direction"] == "out"]


def assert_nothing_sent(sender: LoadedSendHandler) -> None:
    assert out_rows() == [] and rows_with_partition_prefix("SENDRATE#") == [] and sender.providers.calls == []


def put_contact(receiver_address: str, contact_user_id: str, contact_name: str, reach_points: list[dict]) -> None:
    graph.get_table().put_item(Item={"PK": f"RECEIVER#{receiver_address}", "SK": f"CONTACT#{contact_user_id}", "contactName": contact_name,
                                     "contactPwaUserId": contact_user_id, "verifiedVia": "seed", "contactReachPoints": reach_points})


def test_recipient_not_in_the_circle_is_refused(sender):
    sender.send(GRANDMA_USER_ID, Channel.WHATSAPP, "+15551230000")
    sender.send(GRANDMA_USER_ID, Channel.EMAIL, "someone@example.com")
    assert sender.rejection_reasons() == ["not_a_contact", "not_a_contact"]
    assert_nothing_sent(sender)


def test_pending_number_and_typed_phone_are_not_recipients(sender, monkeypatch):
    monkeypatch.setenv("RCS_ENABLED", "true")
    monkeypatch.setenv("SMS_ENABLED", "true")
    graph.get_table().update_item(Key={"PK": f"RECEIVER#{GRANDMA_ADDRESS}", "SK": "CONTACT#jimin"},
                                  UpdateExpression="SET contactReachPoints = list_append(contactReachPoints, :typed)",
                                  ExpressionAttributeValues={":typed": [{"channel": "rcs", "address": TYPED_PHONE, "verifiedVia": "pending"}]})
    minho = circle.ContactForm("Minho", Relationship.SON, (WAITING_NUMBER,), (), ("minho@example.com",))
    assert circle.enrol_contact(GRANDMA_ADDRESS, minho, datetime.now(timezone.utc)).code is not None  # a PENDING row
    sender.send(GRANDMA_USER_ID, Channel.WHATSAPP, f"+{JIMINS_PENDING_DIGITS}")  # Jimin's number still waiting for its code
    sender.send(GRANDMA_USER_ID, Channel.RCS, TYPED_PHONE)
    sender.send(GRANDMA_USER_ID, Channel.SMS, TYPED_PHONE)
    sender.send(GRANDMA_USER_ID, Channel.WHATSAPP, WAITING_NUMBER)
    sender.send(GRANDMA_USER_ID, Channel.EMAIL, "minho@example.com")
    assert sender.rejection_reasons() == ["not_a_contact"] * 5
    assert_nothing_sent(sender)


def test_thirty_first_send_in_an_hour_is_refused(sender):
    for _ in range(31):
        sender.send(GRANDMA_USER_ID, Channel.EMAIL, "jimin@example.com")
    assert sender.rejection_reasons() == ["over_cap"]
    [counter_row] = rows_with_partition_prefix("SENDRATE#")
    assert (counter_row["PK"], counter_row["SK"], counter_row["slotCount"]) == ("SENDRATE#grandma", f"HOUR#{FROZEN_HOUR_LABEL}", 30)
    assert len(sender.providers.calls) == 30 and len(out_rows()) == 30


def test_counter_write_failure_refuses(sender, monkeypatch):
    table = graph.get_table()
    update_item = table.update_item

    def update_item_refusing_send_counters(**arguments):
        if arguments["Key"]["PK"].startswith("SENDRATE#"):
            raise ClientError({"Error": {"Code": "InternalServerError", "Message": "injected"}}, "UpdateItem")
        return update_item(**arguments)

    monkeypatch.setattr(table, "update_item", update_item_refusing_send_counters)
    sender.send(GRANDMA_USER_ID, Channel.EMAIL, "jimin@example.com")
    assert sender.rejection_reasons() == ["over_cap"]
    assert_nothing_sent(sender)


def test_whatsapp_outside_the_window_is_refused(sender):
    sender.inbound.use_reader_scripts({HELLO_TEXT: [verdict_step(NO_ASK_FROM_JIMIN)] * 2})
    sender.send(GRANDMA_USER_ID, Channel.WHATSAPP, JIMIN_NUMBER)  # nothing ever came in from Jimin: no THREAD row
    receive_whatsapp_from_jimin(sender, "wamid.W25", HELLO_TEXT, 25 * SECONDS_PER_HOUR)
    sender.send(GRANDMA_USER_ID, Channel.WHATSAPP, JIMIN_NUMBER)
    assert sender.rejection_reasons() == ["channel_closed", "channel_closed"]
    assert_nothing_sent(sender)
    receive_whatsapp_from_jimin(sender, "wamid.W23", HELLO_TEXT, 23 * SECONDS_PER_HOUR)
    sender.send(GRANDMA_USER_ID, Channel.WHATSAPP, JIMIN_NUMBER)
    assert sender.rejection_reasons() == ["channel_closed", "channel_closed"]
    assert [operation_name for operation_name, _ in sender.providers.calls] == ["SendWhatsAppMessage"]


def test_held_thread_refused_until_answered(sender, monkeypatch):
    monkeypatch.setenv("RCS_ENABLED", "true")
    sender.inbound.use_reader_scripts({NEW_PHONE_TEXT: [verdict_step(JIMIN_MONEY_ASK)], NEW_PHONE_SMS_TEXT: [verdict_step(JIMIN_MONEY_ASK)]})
    receive_whatsapp_from_jimin(sender, "wamid.H1", NEW_PHONE_TEXT, SECONDS_PER_HOUR)  # Jimin's own number asks for money: held
    whatsapp_hold = message_row(Channel.WHATSAPP, "wamid.H1")
    assert whatsapp_hold["state"] == "hold"
    sender.send(GRANDMA_USER_ID, Channel.WHATSAPP, JIMIN_NUMBER)
    assert sender.rejection_reasons() == ["thread_held"] and sender.providers.calls == []
    answer = verdict.record_answer(whatsapp_hold["requestId"], VerdictAnswer.NO, verdict.SenderIdentity(Channel.INAPP, "jimin"),
                                   "ws-no", graph.now_timestamp())
    assert answer.outcome is ActionOutcome.RECORDED
    sender.send(GRANDMA_USER_ID, Channel.WHATSAPP, JIMIN_NUMBER)  # denied: the hold no longer stands
    assert sender.rejection_reasons() == ["thread_held"] and len(sender.providers.calls) == 1
    sender.inbound.handle(SMS_TOPIC_ARN, {**SMS_RECORD, "originationNumber": JIMIN_NUMBER, "inboundMessageId": "sms-2"})
    assert message_row(Channel.SMS, "sms-2")["state"] == "hold"
    sender.send(GRANDMA_USER_ID, Channel.RCS, JIMIN_NUMBER)  # the SMS hold also holds RCS to the same number
    assert sender.rejection_reasons() == ["thread_held", "thread_held"] and len(sender.providers.calls) == 1


def test_from_is_the_callers_own_address(sender):
    sender.send(GRANDMA_USER_ID, Channel.EMAIL, "jimin@example.com", text=f"  {REPLY_TEXT}  ", **{
        "from": "jimin@example.com", "FromEmailAddress": "boss@lookalike.example"})
    assert sender.rejection_reasons() == []
    assert sender.providers.calls == [("SendEmail", {
        "FromEmailAddress": GRANDMA_ADDRESS, "Destination": {"ToAddresses": ["jimin@example.com"]},
        "Content": {"Simple": {"Subject": {"Data": "Message from Grandma"}, "Body": {"Text": {"Data": REPLY_TEXT}}}}})]


def test_provider_failure_keeps_a_failed_row(sender):
    sender.providers.is_failing = True
    sender.send(GRANDMA_USER_ID, Channel.EMAIL, "jimin@example.com")
    assert sender.rejection_reasons() == ["send_failed"] and len(sender.providers.calls) == 1
    [failed_row] = out_rows()
    assert (failed_row["providerStatus"], "providerMessageId" in failed_row) == ("failed", False)
    [(pushed_message, should_remove_gone_connections)] = sender.message_state_pushes(GRANDMA_USER_ID)
    assert (pushed_message["messageId"], pushed_message["providerStatus"]) == (failed_row["PK"].removeprefix("MESSAGE#"), "failed")
    assert should_remove_gone_connections is False
    assert rows_with_partition_prefix("SENDRATE#")[0]["slotCount"] == 1


def test_refusals_before_the_body_is_read(sender):
    unregistered_event = {"requestContext": {"connectionId": "conn-nobody", "messageId": "ws-1"}, "body": "not json"}
    assert send_handler.handle(unregistered_event, None) == {"statusCode": 403}
    put_profile("jimin", {"role": "contact"})
    put_profile("ann", {"role": "org_admin", "orgId": "sunrise"})
    for user_id in ("jimin", "ann"):
        graph.register_connection(f"conn-{user_id}", user_id)
        send_handler.handle({"requestContext": {"connectionId": f"conn-{user_id}", "messageId": "ws-1"}, "body": "not json"}, None)
        sender.send(user_id, Channel.EMAIL, "jimin@example.com")
    assert sender.rejection_reasons() == ["not_allowed"] * 4  # the malformed body is never read for them
    graph.register_connection("conn-grandma", GRANDMA_USER_ID)
    send_handler.handle({"requestContext": {"connectionId": "conn-grandma", "messageId": "ws-1"}, "body": "not json"}, None)
    send_action(send_handler, GRANDMA_USER_ID, {"action": "sync"})  # an action pidge-send has no handler for
    sender.send(GRANDMA_USER_ID, Channel.INAPP, "jimin")
    assert sender.rejection_reasons()[4:] == ["bad_message"] * 3
    assert_nothing_sent(sender)


def test_phone_channels_only_for_the_messenger_resident(sender, monkeypatch):
    seed_resident(BOB_ADDRESS, "bob", "Bob")
    put_contact(BOB_ADDRESS, "sumi", "Sumi", [{"channel": "whatsapp", "address": SUMI_NUMBER, "verifiedVia": "seed"},
                                              {"channel": "email", "address": "sumi@example.com"}])
    sender.send("bob", Channel.WHATSAPP, SUMI_NUMBER)
    sender.send("bob", Channel.EMAIL, "sumi@example.com")
    assert sender.rejection_reasons() == ["not_allowed"]
    assert [arguments["FromEmailAddress"] for _, arguments in sender.providers.calls] == [BOB_ADDRESS]
    sender.inbound.use_reader_scripts({HELLO_TEXT: [verdict_step(NO_ASK_FROM_JIMIN)]})
    receive_whatsapp_from_jimin(sender, "wamid.W1", HELLO_TEXT, SECONDS_PER_HOUR)  # her window to Jimin is open
    monkeypatch.delenv("MESSENGER_RESIDENT_ADDRESS")  # unset: no resident's inbox is on Pidge's numbers
    sender.send(GRANDMA_USER_ID, Channel.WHATSAPP, JIMIN_NUMBER)
    assert sender.rejection_reasons() == ["not_allowed", "not_allowed"] and len(sender.providers.calls) == 1


def test_own_domain_recipient_refused(sender):
    put_contact(GRANDMA_ADDRESS, "desk", "Front Desk", [{"channel": "email", "address": "desk@sendpidge.example"},
                                                        {"channel": "email", "address": "Desk@Mail.SendPidge.example"}])
    sender.send(GRANDMA_USER_ID, Channel.EMAIL, "desk@sendpidge.example")  # the receiving domain: back into Mail Manager
    sender.send(GRANDMA_USER_ID, Channel.EMAIL, "Desk@Mail.SendPidge.example")  # the sending domain
    assert sender.rejection_reasons() == ["not_allowed", "not_allowed"]
    assert_nothing_sent(sender)


def test_sms_refused_while_sms_is_off(sender, monkeypatch):
    sender.send(GRANDMA_USER_ID, Channel.SMS, JIMIN_NUMBER)
    assert sender.rejection_reasons() == ["not_allowed"]
    assert_nothing_sent(sender)
    monkeypatch.setenv("SMS_ENABLED", "true")
    sender.send(GRANDMA_USER_ID, Channel.SMS, JIMIN_NUMBER)  # to Jimin's checked RCS number
    assert sender.rejection_reasons() == ["not_allowed"]
    assert sender.providers.calls == [("SendTextMessage", {"DestinationPhoneNumber": JIMIN_NUMBER, "OriginationIdentity": SMS_PHONE_NUMBER_ARN,
                                                           "MessageBody": REPLY_TEXT, "MessageType": "TRANSACTIONAL"})]


def test_successful_send_writes_sent_row_and_pushes_it(sender):
    sender.inbound.use_reader_scripts({HELLO_TEXT: [verdict_step(NO_ASK_FROM_JIMIN)]})
    receive_whatsapp_from_jimin(sender, "wamid.W1", HELLO_TEXT, SECONDS_PER_HOUR)
    sender.send(GRANDMA_USER_ID, Channel.WHATSAPP, JIMIN_NUMBER)
    assert sender.rejection_reasons() == []
    [(operation_name, arguments)] = sender.providers.calls
    assert (operation_name, arguments["originationPhoneNumberId"], json.loads(arguments["message"])) == (
        "SendWhatsAppMessage", WHATSAPP_PHONE_NUMBER_ID,
        {"messaging_product": "whatsapp", "to": JIMIN_NUMBER, "type": "text", "text": {"body": REPLY_TEXT}})
    [sent_row] = out_rows()
    assert sent_row["PK"].startswith("MESSAGE#whatsapp#out-")
    assert (sent_row["providerStatus"], sent_row["providerMessageId"], sent_row["threadKey"]) == ("sent", "wamid.S1", f"whatsapp#{JIMIN_NUMBER}")
    assert (sent_row["recipientName"], sent_row["recipientAddress"], sent_row["textBody"], sent_row["receiverEmail"]) == (
        "Jimin", JIMIN_NUMBER, REPLY_TEXT, GRANDMA_ADDRESS)
    assert (sent_row["userIndexPK"], sent_row["userIndexSK"]) == ("grandma#MESSAGE", sent_row["sentAt"])
    [(pushed_message, should_remove_gone_connections)] = [
        push for push in sender.message_state_pushes(GRANDMA_USER_ID) if push[0]["direction"] == "out"]
    assert (pushed_message["messageId"], pushed_message["providerStatus"], pushed_message["threadKey"]) == (
        sent_row["PK"].removeprefix("MESSAGE#"), "sent", f"whatsapp#{JIMIN_NUMBER}")
    assert should_remove_gone_connections is False
