"""The in-app safety alert (org_broadcast_send, org_broadcast_ack): only a sender admin linked to a passkey sign-in sends, to
every member of their own org and no one else; the text is bounded and refused with a link, address, handle or long number;
the hourly cap fails closed and no refusal takes a slot; a member answers only her own copy, once; the admin's report and
each resident's Pidge tab read the same rows."""

import json

import pytest
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from pidge_org import handler as org_actions_handler
from shared import channels, graph, org_broadcasts, orgs, profiles
from shared.org_enums import AdminRole
from shared.orgs import AdminSeed, MemberSeed, OrgSeed
from tests.helpers.orgs import seed_test_org
from tests.helpers.profiles import TEST_TIMEZONE, seed_resident
from tests.test_verdict import aws  # noqa: F401
from tests.test_verify_contact import rejection, send_action, verdict_handler_without_profiles  # noqa: F401

SUNRISE_ORG_ID = "sunrise"
GRANDMA_ADDRESS = "grandma@example.com"
HAROLD_ADDRESS = "harold@example.com"
BOB_ADDRESS = "bob@example.com"
FIRE_DRILL_TEXT = "Fire drill at 3 pm today. Please stay in your room until staff knock."
SENT_RATE_PARTITION = f"BROADCASTRATE#{SUNRISE_ORG_ID}"


@pytest.fixture
def sunrise(verdict_handler_without_profiles, monkeypatch):  # noqa: F811
    """Sunrise Commons with residents Grandma and Harold, sender ann (linked to Cognito) and viewer vic (linked); Bob lives at
    Maple Commons. Returns (pidge-verdict, replies to the acting socket, every push_to_user as (user, wire JSON))."""
    handler, pushed_messages = verdict_handler_without_profiles
    monkeypatch.setenv("BROADCASTS_PER_ORG_PER_HOUR", "2")
    user_pushes: list[tuple[str, dict]] = []
    monkeypatch.setattr(channels, "push_to_user", lambda user_id, message, should_remove_gone_connections=True: user_pushes.append(
        (user_id, json.loads(channels.encode_push_message(message)))) or 1)
    seed_resident(GRANDMA_ADDRESS, "grandma", "Grandma", org_id=SUNRISE_ORG_ID)
    seed_resident(HAROLD_ADDRESS, "harold", "Harold", org_id=SUNRISE_ORG_ID)
    seed_resident(BOB_ADDRESS, "bob", "Bob", org_id="maple")
    sunrise_seed = OrgSeed(SUNRISE_ORG_ID, "Sunrise Commons", ("office@sunrise.example",), (), TEST_TIMEZONE,
                           (AdminSeed("ann", AdminRole.SENDER), AdminSeed("vic", AdminRole.VIEWER)),
                           (MemberSeed(GRANDMA_ADDRESS, ()), MemberSeed(HAROLD_ADDRESS, ())))
    orgs.write_org_seed(orgs.validate_org_seed(sunrise_seed))
    seed_test_org("maple", ["max"], [BOB_ADDRESS], admin_role=AdminRole.SENDER)
    for admin_user_id in ("ann", "vic", "max"):
        link_to_cognito(admin_user_id)
    return handler, pushed_messages, user_pushes


def link_to_cognito(user_id: str) -> None:
    """What scripts/seed_auth_users.py link writes on the PROFILE (its SUB# row plays no part here)."""
    graph.get_table().update_item(Key={"PK": f"USER#{user_id}", "SK": "PROFILE"}, UpdateExpression="SET #sub = :sub",
                                  ExpressionAttributeNames={"#sub": profiles.COGNITO_SUB_FIELD},
                                  ExpressionAttributeValues={":sub": f"sub-{user_id}"})


def send_alert(user_id: str, text) -> None:
    send_action(org_actions_handler, user_id, {"action": "org_broadcast_send", "text": text})


def read_broadcast_rows(org_id: str = SUNRISE_ORG_ID) -> list[dict]:
    index_rows = graph.get_table().query(KeyConditionExpression=Key("PK").eq(f"ORG#{org_id}") & Key("SK").begins_with("BROADCASTIDX#"))
    return index_rows["Items"]


def read_rate_counter_rows() -> list[dict]:
    return graph.get_table().query(KeyConditionExpression=Key("PK").eq(SENT_RATE_PARTITION))["Items"]


def sync_of(handler, pushed_messages, user_id: str) -> dict:
    pushed_messages.clear()
    send_action(handler, user_id, {"action": "sync"})
    return pushed_messages[-1]


def test_sender_alert_reaches_every_member_and_the_report(sunrise):
    handler, pushed_messages, user_pushes = sunrise
    send_alert("ann", f"  {FIRE_DRILL_TEXT}\n")
    assert pushed_messages == []  # sent: success has no reply of its own
    [index_row] = read_broadcast_rows()
    broadcast_id = index_row["broadcastId"]
    sent_at = index_row["SK"].split("#")[1]
    notice = {"broadcastId": broadcast_id, "orgName": "Sunrise Commons", "text": FIRE_DRILL_TEXT, "sentAt": sent_at, "answer": None}
    assert user_pushes == [("grandma", {"type": "org_notice", "notice": notice}), ("harold", {"type": "org_notice", "notice": notice}),
                           ("org-sunrise", {"type": "org_changed"})]
    assert sync_of(handler, pushed_messages, "grandma")["orgNotices"] == [notice]
    assert sync_of(handler, pushed_messages, "bob")["orgNotices"] == []  # another home's resident gets nothing
    admin_sync = sync_of(handler, pushed_messages, "vic")  # a viewer reads the report
    assert admin_sync["adminRole"] == "viewer"
    assert admin_sync["org"]["broadcasts"] == [{
        "broadcastId": broadcast_id, "kind": "safety_alert", "text": FIRE_DRILL_TEXT, "sentAt": sent_at, "recipients": [
            {"memberRef": GRANDMA_ADDRESS, "residentName": "Grandma", "state": "sent", "answer": None},
            {"memberRef": HAROLD_ADDRESS, "residentName": "Harold", "state": "sent", "answer": None}]}]
    assert sync_of(handler, pushed_messages, "max")["org"]["broadcasts"] == []  # Maple's admin sees none of Sunrise's alerts
    [audit_row] = [row for row in graph.get_table().query(KeyConditionExpression=Key("PK").eq(f"ORGAUDIT#{SUNRISE_ORG_ID}"))["Items"]
                   if row["action"] == "broadcast_send"]
    assert (audit_row["adminId"], audit_row["targetRef"]) == ("ann", broadcast_id)


def test_each_member_answers_her_own_copy_once(sunrise):
    handler, pushed_messages, user_pushes = sunrise
    send_alert("ann", FIRE_DRILL_TEXT)
    [index_row] = read_broadcast_rows()
    broadcast_id = index_row["broadcastId"]
    user_pushes.clear()
    send_action(handler, "grandma", {"action": "org_broadcast_ack", "broadcastId": broadcast_id, "answer": "need_help"})
    send_action(handler, "harold", {"action": "org_broadcast_ack", "broadcastId": broadcast_id, "answer": "got_it"})
    assert pushed_messages == []
    assert [(user_id, message["type"], message.get("notice", {}).get("answer")) for user_id, message in user_pushes] == [
        ("grandma", "org_notice", "need_help"), ("org-sunrise", "org_changed", None),
        ("harold", "org_notice", "got_it"), ("org-sunrise", "org_changed", None)]

    send_action(handler, "grandma", {"action": "org_broadcast_ack", "broadcastId": broadcast_id, "answer": "got_it"})
    assert pushed_messages == [rejection("not_applied")]  # the first answer wins
    send_action(handler, "bob", {"action": "org_broadcast_ack", "broadcastId": broadcast_id, "answer": "got_it"})
    assert pushed_messages[-1] == rejection("not_allowed")  # not his home's alert: his key is under Maple, where there is none
    for malformed_body in ({"broadcastId": "bc-1", "answer": "got_it"}, {"broadcastId": broadcast_id, "answer": "yes"}):
        send_action(handler, "harold", {"action": "org_broadcast_ack", **malformed_body})
        assert pushed_messages[-1] == rejection("bad_message")
    send_action(org_actions_handler, "ann", {"action": "org_broadcast_ack", "broadcastId": broadcast_id, "answer": "got_it"})
    assert pushed_messages[-1] == rejection("bad_message")  # a resident action is not an org route

    recipients = sync_of(handler, pushed_messages, "ann")["org"]["broadcasts"][0]["recipients"]
    assert [(recipient["residentName"], recipient["state"], recipient["answer"]) for recipient in recipients] == [
        ("Grandma", "acknowledged", "need_help"), ("Harold", "acknowledged", "got_it")]
    assert sync_of(handler, pushed_messages, "grandma")["orgNotices"][0]["answer"] == "need_help"


@pytest.mark.parametrize("refused_sender", ["vic", "ann-unlinked", "grandma"])
def test_only_a_linked_sender_admin_sends(sunrise, refused_sender):
    _handler, pushed_messages, user_pushes = sunrise
    user_id = refused_sender
    if refused_sender == "ann-unlinked":
        user_id = "ann"
        graph.get_table().update_item(Key={"PK": "USER#ann", "SK": "PROFILE"}, UpdateExpression="REMOVE #sub",
                                      ExpressionAttributeNames={"#sub": profiles.COGNITO_SUB_FIELD})
    send_alert(user_id, FIRE_DRILL_TEXT)
    assert pushed_messages == [rejection("not_allowed")]
    assert (read_broadcast_rows(), read_rate_counter_rows(), user_pushes) == ([], [], [])


@pytest.mark.parametrize(("text", "reason"), [
    ("Pay the fee at sunrise-help.com today.", "text_refused"),
    ("Call the desk on 555 123 4567.", "text_refused"),
    ("Write to billing@sunrise-help.example.", "text_refused"),
    ("   ", "bad_message"),
    ("x" * (org_broadcasts.ALERT_TEXT_MAX_CHARACTERS + 1), "bad_message"),
    (["not", "text"], "bad_message"),
])
def test_alert_text_is_bounded_and_never_carries_a_link(sunrise, text, reason):
    _handler, pushed_messages, user_pushes = sunrise
    send_alert("ann", text)
    assert pushed_messages == [rejection(reason)]
    assert (read_broadcast_rows(), read_rate_counter_rows(), user_pushes) == ([], [], [])


def test_hourly_cap_and_a_failed_counter_both_refuse(sunrise, monkeypatch):
    _handler, pushed_messages, _user_pushes = sunrise
    send_alert("ann", FIRE_DRILL_TEXT)
    send_alert("ann", "The lift is working again.")
    send_alert("ann", "Lunch moves to the garden room.")
    assert pushed_messages == [rejection("over_cap")]
    assert len(read_broadcast_rows()) == 2

    def refuse_every_update(**_arguments):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow down"}}, "UpdateItem")

    monkeypatch.setattr(graph.get_table(), "update_item", refuse_every_update)
    monkeypatch.setenv("BROADCASTS_PER_ORG_PER_HOUR", "50")
    pushed_messages.clear()
    send_alert("ann", "Lunch moves to the garden room.")
    assert pushed_messages == [rejection("over_cap")]  # the counter could not be written: refused, never a free pass
    assert len(read_broadcast_rows()) == 2
