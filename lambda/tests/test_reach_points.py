"""One reader for a contact's numbers and emails (checklist 10e): a seeded row (the single contactWhatsApp / contactRcs /
contactEmail fields) and a contactReachPoints row holding the same points ask, accept answers and light chips the same."""

import asyncio
from pathlib import Path

import pytest

from shared import channels, graph, payloads, reach_points, verdict
from shared.enums import Channel, VerifiedVia

JIMIN_WHATSAPP_NUMBER = "+821012345678"
JIMIN_RCS_NUMBER = "+821087654321"
JIMIN_EMAIL = "jimin@gmail.com"
STRANGER_NUMBER = "+821099999999"
ALL_CHANNELS_ON = payloads.ReachFlags(is_rcs_enabled=True, is_whatsapp_enabled=True, is_email_ask_enabled=True)
OLD_CONTACT_FIELDS = ("contactWhatsApp", "contactRcs", "contactEmail")
LAMBDA_SOURCE_DIRECTORY = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = LAMBDA_SOURCE_DIRECTORY.parent

SEEDED_ROW = {
    "PK": "RECEIVER#grandma@sendpidge.com", "SK": "CONTACT#jimin", "contactName": "Jimin", "contactPwaUserId": "jimin",
    "verifiedVia": "seed", "contactWhatsApp": JIMIN_WHATSAPP_NUMBER, "contactRcs": JIMIN_RCS_NUMBER, "contactEmail": JIMIN_EMAIL,
    "knownAddresses": [JIMIN_EMAIL],
}
LIST_ROW = {
    "PK": "RECEIVER#grandma@sendpidge.com", "SK": "CONTACT#jimin", "contactName": "Jimin", "contactPwaUserId": "jimin",
    "verifiedVia": "seed", "knownAddresses": [JIMIN_EMAIL],
    "contactReachPoints": [
        {"channel": "whatsapp", "address": JIMIN_WHATSAPP_NUMBER, "verifiedVia": "seed"},
        {"channel": "rcs", "address": JIMIN_RCS_NUMBER, "verifiedVia": "seed"},
        {"channel": "email", "address": JIMIN_EMAIL},
    ],
}


def recorded_fan_out(contact_row: dict, monkeypatch) -> tuple[list[tuple[str, str]], list[Channel]]:
    """Runs the production fan-out with every flag on; each sender only records where it would have sent. Returns the sends
    and the channels the request records as sent."""
    sends: list[tuple[str, str]] = []

    def recorder(channel: Channel):
        def send(request_row, contact, receiver_name, destination):
            sends.append((str(channel), destination))
            return channel
        return send

    for flag_name in ("RCS_ENABLED", "WHATSAPP_ENABLED", "EMAIL_ASK_ENABLED"):
        monkeypatch.setattr(channels, flag_name, True)
    monkeypatch.setattr(channels, "ask_in_app", recorder(Channel.INAPP))
    monkeypatch.setattr(channels, "ask_on_rcs", recorder(Channel.RCS))
    monkeypatch.setattr(channels, "ask_on_whatsapp", recorder(Channel.WHATSAPP))
    monkeypatch.setattr(channels, "ask_on_email", recorder(Channel.EMAIL))
    channels_sent = asyncio.run(channels.ask_on_every_channel({"PK": "REQUEST#req-1"}, graph.contact_from_row(contact_row), "Grandma"))
    return sorted(sends), channels_sent


def test_a_seeded_row_and_a_list_row_read_as_the_same_points():
    assert reach_points.reach_points_of(SEEDED_ROW) == reach_points.reach_points_of(LIST_ROW) == (
        reach_points.ReachPoint(Channel.WHATSAPP, JIMIN_WHATSAPP_NUMBER, VerifiedVia.SEED),
        reach_points.ReachPoint(Channel.RCS, JIMIN_RCS_NUMBER, VerifiedVia.SEED),
        reach_points.ReachPoint(Channel.EMAIL, JIMIN_EMAIL, None),
    )


def test_both_rows_ask_the_same_addresses_on_every_channel(monkeypatch):
    expected_sends = sorted([("inapp", "jimin"), ("whatsapp", JIMIN_WHATSAPP_NUMBER), ("rcs", JIMIN_RCS_NUMBER), ("email", JIMIN_EMAIL)])
    assert recorded_fan_out(SEEDED_ROW, monkeypatch)[0] == expected_sends
    assert recorded_fan_out(LIST_ROW, monkeypatch)[0] == expected_sends


@pytest.mark.parametrize("sender", [
    verdict.SenderIdentity(Channel.WHATSAPP, JIMIN_WHATSAPP_NUMBER),
    verdict.SenderIdentity(Channel.RCS, JIMIN_RCS_NUMBER),
    verdict.SenderIdentity(Channel.EMAIL, "Jimin@Gmail.com"),
    verdict.SenderIdentity(Channel.INAPP, "jimin"),
    verdict.SenderIdentity(Channel.WHATSAPP, STRANGER_NUMBER),
    verdict.SenderIdentity(Channel.RCS, JIMIN_WHATSAPP_NUMBER),
    verdict.SenderIdentity(Channel.EMAIL, "someone@else.example"),
    verdict.SenderIdentity(Channel.INAPP, "someone-else"),
])
def test_both_rows_accept_the_same_answerers(sender):
    assert verdict.is_expected_sender(SEEDED_ROW, sender) == verdict.is_expected_sender(LIST_ROW, sender)


def test_the_known_answerers_are_accepted_and_strangers_refused():
    assert verdict.is_expected_sender(LIST_ROW, verdict.SenderIdentity(Channel.WHATSAPP, JIMIN_WHATSAPP_NUMBER))
    assert verdict.is_expected_sender(LIST_ROW, verdict.SenderIdentity(Channel.EMAIL, "Jimin@Gmail.com"))
    assert not verdict.is_expected_sender(LIST_ROW, verdict.SenderIdentity(Channel.WHATSAPP, STRANGER_NUMBER))
    assert not verdict.is_expected_sender(None, verdict.SenderIdentity(Channel.WHATSAPP, JIMIN_WHATSAPP_NUMBER))


def test_both_rows_light_the_same_circle_chips():
    seeded_entry = payloads.circle_entry_for_receiver(SEEDED_ROW, ALL_CHANNELS_ON)
    assert seeded_entry["reach"] == payloads.circle_entry_for_receiver(LIST_ROW, ALL_CHANNELS_ON)["reach"]
    assert seeded_entry["reach"] == {"inapp": True, "rcs": True, "whatsapp": True, "email": True}


def test_both_rows_get_the_receipt_at_the_same_address():
    assert verdict.receipt_address_of(SEEDED_ROW) == verdict.receipt_address_of(LIST_ROW) == JIMIN_EMAIL


def test_a_pending_number_is_never_asked_never_an_answerer_and_lights_no_chip(monkeypatch):
    pending_row = {**LIST_ROW, "contactReachPoints": [{"channel": "whatsapp", "address": JIMIN_WHATSAPP_NUMBER, "verifiedVia": "pending"},
                                                      {"channel": "rcs", "address": JIMIN_RCS_NUMBER, "verifiedVia": "pending"}]}
    assert recorded_fan_out(pending_row, monkeypatch)[0] == [("inapp", "jimin")]
    assert not verdict.is_expected_sender(pending_row, verdict.SenderIdentity(Channel.WHATSAPP, JIMIN_WHATSAPP_NUMBER))
    assert not verdict.is_expected_sender(pending_row, verdict.SenderIdentity(Channel.RCS, JIMIN_RCS_NUMBER))
    assert payloads.circle_entry_for_receiver(pending_row, ALL_CHANNELS_ON)["reach"] == {
        "inapp": True, "rcs": False, "whatsapp": False, "email": False}


@pytest.mark.parametrize("broken_point", [
    {"channel": "whatsapp", "address": JIMIN_WHATSAPP_NUMBER},                         # a number with no check state
    {"channel": "inapp", "address": "jimin"},                                          # the app is the PWA token, not a point
    {"channel": "fax", "address": "+82212345678", "verifiedVia": "seed"},
    {"channel": "whatsapp", "address": JIMIN_WHATSAPP_NUMBER, "verifiedVia": "maybe"},
])
def test_a_malformed_point_raises(broken_point):
    with pytest.raises(ValueError):
        reach_points.reach_points_of({**LIST_ROW, "contactReachPoints": [broken_point]})


def test_number_siblings_reach_through_rcs():
    assert [reach_points.reach_channel_of(channel) for channel in (Channel.SMS, Channel.VOICE)] == [Channel.RCS, Channel.RCS]
    for channel in (Channel.WHATSAPP, Channel.RCS, Channel.EMAIL):
        assert reach_points.reach_channel_of(channel) is channel
    with pytest.raises(ValueError):
        reach_points.reach_channel_of(Channel.INAPP)


def test_only_the_reader_and_the_seed_script_name_the_old_fields():
    allowed_files = {LAMBDA_SOURCE_DIRECTORY / "shared" / "reach_points.py", REPOSITORY_ROOT / "scripts" / "seed_graph.py"}
    source_files = [path for directory in ("lambda", "scripts", "agent") for path in (REPOSITORY_ROOT / directory).rglob("*.py")]
    offenders = sorted(
        f"{source_file.relative_to(REPOSITORY_ROOT)}: {field_name}"
        for source_file in source_files
        if "tests" not in source_file.parts and ".venv" not in source_file.parts and source_file not in allowed_files
        for field_name in OLD_CONTACT_FIELDS if f'"{field_name}"' in source_file.read_text()
    )
    assert offenders == []
