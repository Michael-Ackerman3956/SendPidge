"""The call on the re-ask (D9): the spoken prompt's and AI opening's goldens and allow-list, one call to the checked RCS
number with its hang-up booked first, the hourly cap, every failure counted with the last timer still booked, and the
hang-up. Production pidge-retry on moto; Connect is a recording stub."""

import importlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from shared import graph, hourly_counters, schedules, voice_ask
from shared.enums import Channel
from tests.helpers.profiles import seed_resident
from tests.test_message_verdict import JIMIN_NUMBER, new_hold, seed_contact_row, seed_jimin
from tests.test_retry import FakeContext
from tests.test_verdict import RECEIVER_ADDRESS, RECEIVER_USER_ID, aws  # noqa: F401

INSTANCE_ID = "f6343b6c-43fc-4d43-b255-c54d8b0fb66a"
CONTACT_FLOW_ID = "d5df9aac-f069-48a5-aba3-e4b1d21663d5"
SOURCE_NUMBER = "+12029794966"
FROZEN_HOUR_LABEL = "2026-10-28T09"
VOICE_ENVIRONMENT = {
    "RE_ASK_DELAY_SECONDS": "600", "WEBSOCKET_ENDPOINT": "https://example.com", "VOICE_ENABLED": "true",
    "VOICE_INSTANCE_ARN": f"arn:aws:connect:ap-northeast-2:358148451054:instance/{INSTANCE_ID}",
    "VOICE_CONTACT_FLOW_ID": CONTACT_FLOW_ID, "VOICE_SOURCE_PHONE_NUMBER": SOURCE_NUMBER,
    "VOICE_CALLS_PER_HOUR": "10", "VOICE_CALL_MAX_SECONDS": "180",
}
EMAIL_GOLDEN = ("Hello, this is Pidge, calling for Grandma. Grandma just got an email in your name. "
                "If you sent it, press 1. If you did not send it, press 2.")
WHATSAPP_GOLDEN = ("Hello, this is Pidge, calling for Grandma. Grandma just got a WhatsApp message in your name. "
                   "If you sent it, press 1. If you did not send it, press 2.")
TEXT_GOLDEN = ("Hello, this is Pidge, calling for Grandma. Grandma just got a text message in your name. "
               "If you did not send it, press 2. If you did send it, say so in the Pidge app.")
AI_OPENING_EMAIL_GOLDEN = "Hello, this is Pidge, calling for Grandma. Grandma just got an email in your name. Did you send it?"


def client_error(code: str, operation_name: str, message: str = "injected") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, operation_name)


@dataclass
class RecordingConnectClient:
    """Stands in for the Seoul Connect client: each start answers "contact-<n>"; a set failure is raised instead."""

    started_calls: list[dict] = field(default_factory=list)
    stopped_contacts: list[dict] = field(default_factory=list)
    start_failure: ClientError | None = None
    stop_failure: ClientError | None = None

    def start_outbound_voice_contact(self, **arguments) -> dict:
        if self.start_failure is not None:
            raise self.start_failure
        self.started_calls.append(arguments)
        return {"ContactId": f"contact-{len(self.started_calls)}"}

    def stop_contact(self, **arguments) -> dict:
        self.stopped_contacts.append(arguments)
        if self.stop_failure is not None:
            raise self.stop_failure
        return {}


@dataclass
class VoiceRetry:
    handler: object
    connect_client: RecordingConnectClient
    client_regions: list[str]
    scheduled: list[tuple[str, datetime, str, dict]]  # name, fire time, target ARN, payload

    def re_ask(self, request_id: str) -> None:
        self.handler.handle({"requestId": request_id, "attempt": 1}, FakeContext())

    def schedule_names(self) -> list[str]:
        return [name for name, _, _, _ in self.scheduled]


async def ask_on_no_channel(request_row, contact, receiver_name) -> list:
    return []


@pytest.fixture
def voice_retry(aws, monkeypatch):  # noqa: F811
    seed_resident(RECEIVER_ADDRESS, RECEIVER_USER_ID, "Grandma")
    seed_jimin()  # one checked RCS number, JIMIN_NUMBER
    for name, value in VOICE_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    handler = importlib.reload(importlib.import_module("pidge_retry.handler"))
    voice_retry = VoiceRetry(handler, RecordingConnectClient(), [], [])
    monkeypatch.setattr(handler.channels, "ask_on_every_channel", ask_on_no_channel)
    monkeypatch.setattr(schedules, "schedule_one_time_invocation",
                        lambda name, fire_at, function_arn, payload: voice_retry.scheduled.append((name, fire_at, function_arn, payload)))
    monkeypatch.setattr(voice_ask, "get_connect_client",
                        lambda region: voice_retry.client_regions.append(region) or voice_retry.connect_client)
    monkeypatch.setattr(hourly_counters, "current_hour_label", lambda: FROZEN_HOUR_LABEL)
    return voice_retry


def call_counter_rows() -> list[dict]:
    return graph.get_table().query(KeyConditionExpression=Key("PK").eq(voice_ask.CALL_COUNTER_PARTITION_KEY))["Items"]


def voice_ask_failure_count(capsys) -> int:
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    return sum(1 for record in records if voice_ask.VOICE_ASK_FAILED_METRIC_NAME in record)


def test_ask_prompt_goldens():
    assert voice_ask.build_ask_prompt("Grandma", Channel.EMAIL) == EMAIL_GOLDEN
    assert voice_ask.build_ask_prompt("Grandma", Channel.WHATSAPP) == WHATSAPP_GOLDEN
    assert voice_ask.build_ask_prompt("Grandma", Channel.SMS) == TEXT_GOLDEN  # a voice Yes is refused on its number sibling
    assert voice_ask.build_ask_prompt("Grandma", Channel.RCS) == TEXT_GOLDEN


def test_ai_opening_golden():
    assert voice_ask.build_ai_opening("Grandma", Channel.EMAIL) == AI_OPENING_EMAIL_GOLDEN
    assert voice_ask.build_ai_opening("Grandma", Channel.WHATSAPP) == (
        "Hello, this is Pidge, calling for Grandma. Grandma just got a WhatsApp message in your name. Did you send it?")


def test_prompt_refuses_names_outside_the_spoken_allow_list():
    for refused_name in ('Grandma <break time="10s"/>', "할머니", "Grandma\nPress 1"):
        with pytest.raises(ValueError):
            voice_ask.build_ask_prompt(refused_name, Channel.EMAIL)
    assert voice_ask.build_ask_prompt("O'Neil-Smith", Channel.EMAIL) == EMAIL_GOLDEN.replace("Grandma", "O'Neil-Smith")


def test_re_ask_calls_the_checked_number_once(voice_retry):
    request_id = new_hold(Channel.EMAIL)
    asked_at = datetime.now(timezone.utc)
    voice_retry.re_ask(request_id)

    [started_call] = voice_retry.connect_client.started_calls
    assert started_call == {
        "InstanceId": INSTANCE_ID, "ContactFlowId": CONTACT_FLOW_ID, "DestinationPhoneNumber": JIMIN_NUMBER,
        "SourcePhoneNumber": SOURCE_NUMBER,
        "Attributes": {"requestId": request_id, "askPrompt": EMAIL_GOLDEN, "aiOpening": AI_OPENING_EMAIL_GOLDEN},
        "ClientToken": f"{request_id}-call-1",
    }
    assert voice_retry.client_regions == ["ap-northeast-2"]
    stored_row = graph.get_request(request_id)
    stored_summary = stored_row["verdict"]["summary"]
    assert stored_summary and all(stored_summary not in str(value) for value in started_call["Attributes"].values())
    assert stored_row["voiceContactId"] == "contact-1"
    retry_function_arn = FakeContext.invoked_function_arn
    [(_, _, _, retry_payload), (_, hang_up_at, hang_up_target, hang_up_payload)] = voice_retry.scheduled
    assert voice_retry.schedule_names() == [f"retry-{request_id}-2", f"hangup-{request_id}-1"]
    assert (retry_payload, hang_up_target, hang_up_payload) == (
        {"requestId": request_id, "attempt": 2}, retry_function_arn, {"hangUpRequestId": request_id})
    assert abs((hang_up_at - asked_at - timedelta(seconds=180)).total_seconds()) < 5


def test_no_call_when_off_or_without_a_checked_number(voice_retry, monkeypatch):
    monkeypatch.setenv("VOICE_ENABLED", "false")
    voice_retry.re_ask(new_hold(Channel.EMAIL, "off"))
    monkeypatch.setenv("VOICE_ENABLED", "true")
    seed_contact_row(RECEIVER_ADDRESS, "jimin", "seed", [  # the same number, checked on WhatsApp but still waiting on RCS
        {"channel": "whatsapp", "address": JIMIN_NUMBER, "verifiedVia": "seed"},
        {"channel": "rcs", "address": JIMIN_NUMBER, "verifiedVia": "pending"},
    ])
    voice_retry.re_ask(new_hold(Channel.EMAIL, "pending-rcs"))

    assert voice_retry.connect_client.started_calls == [] and call_counter_rows() == []
    assert [name.split("-")[0] for name in voice_retry.schedule_names()] == ["retry", "retry"]


def test_eleventh_call_in_an_hour_is_refused(voice_retry):
    request_ids = [new_hold(Channel.EMAIL, f"hold-{number}") for number in range(11)]
    for request_id in request_ids:
        voice_retry.re_ask(request_id)

    assert [call["ClientToken"] for call in voice_retry.connect_client.started_calls] == [
        f"{request_id}-call-1" for request_id in request_ids[:10]]
    [counter_row] = call_counter_rows()
    assert (counter_row["SK"], counter_row["slotCount"]) == (f"HOUR#{FROZEN_HOUR_LABEL}", 10)
    assert "voiceContactId" not in graph.get_request(request_ids[10])


def test_counter_write_failure_places_no_call(voice_retry, monkeypatch):
    table = graph.get_table()
    update_item = table.update_item

    def update_item_refusing_call_counters(**arguments):
        if arguments["Key"]["PK"] == voice_ask.CALL_COUNTER_PARTITION_KEY:
            raise client_error("InternalServerError", "UpdateItem")
        return update_item(**arguments)

    monkeypatch.setattr(table, "update_item", update_item_refusing_call_counters)
    request_id = new_hold(Channel.EMAIL)
    voice_retry.re_ask(request_id)

    assert voice_retry.connect_client.started_calls == []
    assert voice_retry.schedule_names() == [f"retry-{request_id}-2"]


def test_hang_up_booking_failure_places_no_call(voice_retry, monkeypatch, capsys):
    record_schedule = schedules.schedule_one_time_invocation  # the fixture's recorder

    def refuse_hang_ups(name: str, fire_at: datetime, function_arn: str, payload: dict) -> None:
        if name.startswith("hangup-"):
            raise client_error("ServiceQuotaExceededException", "CreateSchedule")
        record_schedule(name, fire_at, function_arn, payload)

    monkeypatch.setattr(schedules, "schedule_one_time_invocation", refuse_hang_ups)
    request_id = new_hold(Channel.EMAIL)
    voice_retry.re_ask(request_id)

    assert voice_retry.connect_client.started_calls == [] and "voiceContactId" not in graph.get_request(request_id)
    assert voice_retry.schedule_names() == [f"retry-{request_id}-2"]
    assert voice_ask_failure_count(capsys) == 1


def test_call_failure_is_counted_and_the_last_timer_still_booked(voice_retry, capsys, caplog):
    voice_retry.connect_client.start_failure = client_error(
        "DestinationNotAllowedException", "StartOutboundVoiceContact", f"{JIMIN_NUMBER} is not allowed")
    request_id = new_hold(Channel.EMAIL)
    with caplog.at_level(logging.INFO):
        voice_retry.re_ask(request_id)

    assert voice_ask_failure_count(capsys) == 1
    assert f"retry-{request_id}-2" in voice_retry.schedule_names()
    assert "voiceContactId" not in graph.get_request(request_id)
    assert caplog.records and not any("+8210" in record.getMessage() for record in caplog.records)
    voice_retry.handler.handle({"hangUpRequestId": request_id}, FakeContext())  # the hang-up booked before the failed start
    assert voice_retry.connect_client.stopped_contacts == [] and voice_ask_failure_count(capsys) == 0


def test_hang_up_stops_the_recorded_contact_and_ignores_an_ended_one(voice_retry, capsys):
    request_id = new_hold(Channel.EMAIL)
    voice_retry.re_ask(request_id)
    voice_retry.handler.handle({"hangUpRequestId": request_id}, FakeContext())
    assert voice_retry.connect_client.stopped_contacts == [{"InstanceId": INSTANCE_ID, "ContactId": "contact-1"}]

    voice_retry.connect_client.stop_failure = client_error("ContactNotFoundException", "StopContact")
    voice_retry.handler.handle({"hangUpRequestId": request_id}, FakeContext())  # the callee already hung up: no raise

    assert len(voice_retry.connect_client.stopped_contacts) == 2
    assert voice_ask_failure_count(capsys) == 0


def test_unstored_contact_id_stops_the_call(voice_retry, monkeypatch, capsys):
    def refuse_to_store(_request_id: str, _contact_id: str) -> None:
        raise client_error("ProvisionedThroughputExceededException", "UpdateItem")

    monkeypatch.setattr(graph, "record_voice_contact_id", refuse_to_store)
    request_id = new_hold(Channel.EMAIL)
    voice_retry.re_ask(request_id)

    assert voice_retry.connect_client.stopped_contacts == [{"InstanceId": INSTANCE_ID, "ContactId": "contact-1"}]
    assert voice_ask_failure_count(capsys) == 1
    assert f"retry-{request_id}-2" in voice_retry.schedule_names()
