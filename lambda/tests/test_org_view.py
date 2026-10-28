"""The safety overview reads only what the projection wrote: an ORGLOG# row holds an event's name, time, channel and ids and
nothing else; a member whose coverage fact shares nothing reads Not shared whatever the log holds; a sharing member with no
open episode reads Nothing open; every state has an exit event; the dashboard turns stale when the canary stops; an episode
reopened after a resolve can be resolved again, and only a failed RESOLVE# condition reads as not applied."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from pidge_org import handler as org_actions_handler
from pidge_org_projection import handler as projection_handler
from shared import channels, graph, org_view, orgs
from shared.org_enums import MemberSafetyState, OrgEvent
from shared.org_events import CANARY_SORT_KEY, ORG_LOG_PARTITION_PREFIX, ProjectedEvent, episode_state
from tests.helpers.stream_records import read_stored_row, stream_event, stream_record_for_row
from tests.test_org import (
    GRANDMA_ADDRESS, SUNRISE_ORG_ID, add_office_to_circle, new_office_request, rejection, seed_sunrise_and_maple,
)
from tests.test_projection import TRANSACTION_ERRORS, time_out
from tests.test_verdict import aws  # noqa: F401
from tests.test_verify_contact import send_action, verdict_handler_without_profiles  # noqa: F401

SUNRISE_PREFERENCE_KEY = {"PK": f"RECEIVER#{GRANDMA_ADDRESS}", "SK": f"ORGPREF#{SUNRISE_ORG_ID}"}
CANARY_KEY = {"PK": f"ORG#{SUNRISE_ORG_ID}", "SK": CANARY_SORT_KEY}
ORG_LOG_REQUEST_EVENT_FIELDS = {"PK", "SK", "memberRef", "event", "sourceKey", "at", "requestId", "ttlEpochSeconds"}
CHANNEL_BY_EVENT = {"message_held": "email", "contact_confirmed_not_me": "inapp"}  # how it came in, how the answer came back
DASHBOARD_FIELDS = {"orgId", "orgName", "lastProjectedAt", "isStale", "members", "actionable", "coverage", "callList", "broadcasts"}
OFFICE_EMAIL_TEXTS = ("Invoice", "Asks to pay an invoice.", "billing@sunrise-help.example", "Sunrise Commons")
EPISODE_SOURCE_KEY = "REQUEST#req-00000000-0000-4000-8000-000000000000"
ACTIONABLE_FIELDS = {"memberRef", "residentName", "sourceKey", "state", "at"}
RESOLVE_ROW_TTL_SECONDS = 90 * 24 * 3600  # a RESOLVE# row and its org_resolved log row are kept 90 days from its at


@pytest.fixture
def sunrise(verdict_handler_without_profiles, monkeypatch):  # noqa: F811
    """Sunrise and Maple seeded; pidge-verdict for the residents' and admins' own actions; the projection's org_changed
    pushes go nowhere."""
    handler, pushed_messages = verdict_handler_without_profiles
    monkeypatch.setattr(channels, "push_to_user", lambda _user_id, _message, should_remove_gone_connections=True: 0)
    seed_sunrise_and_maple()
    return handler, pushed_messages


def project(*records: dict) -> None:
    """Runs the stream records through the projection; every one must succeed."""
    assert projection_handler.handle(stream_event(list(records)), None) == {"batchItemFailures": []}


def project_inserted(*keys: dict) -> None:
    """Projects each row as its production writer stored it, as an INSERT."""
    project(*(stream_record_for_row(key, None) for key in keys))


def request_key(request_id: str) -> dict:
    return {"PK": f"REQUEST#{request_id}", "SK": "META"}


def share_with_the_office(handler, visibility: str = "alerts_only") -> None:
    """Grandma's own choice, projected into her MEMBER# coverage fact."""
    send_action(handler, "grandma", {"action": "set_org_visibility", "visibility": visibility})
    project_inserted(SUNRISE_PREFERENCE_KEY)


def read_org_log_rows() -> list[dict]:
    return graph.get_table().query(KeyConditionExpression=Key("PK").eq(f"{ORG_LOG_PARTITION_PREFIX}{SUNRISE_ORG_ID}"))["Items"]


def sunrise_dashboard(now: datetime) -> dict:
    return org_view.org_dashboard_for(orgs.require_org_admin("ann"), now)


def read_resolve_rows(source_key: str) -> list[dict]:
    """The episode's ORG#<orgId>/RESOLVE#<sourceKey>#<at> rows, oldest first."""
    return graph.get_table().query(KeyConditionExpression=Key("PK").eq(f"ORG#{SUNRISE_ORG_ID}")
                                   & Key("SK").begins_with(f"RESOLVE#{source_key}#"))["Items"]


def key_of(row: dict) -> dict:
    return {"PK": row["PK"], "SK": row["SK"]}


def test_projection_keys_are_enum_name_time_channel_only(sunrise):
    handler, pushed_messages = sunrise
    share_with_the_office(handler)
    office = add_office_to_circle(GRANDMA_ADDRESS, SUNRISE_ORG_ID)
    capped_id, denied_id = (new_office_request(f"inbox/office-{number}.eml", office) for number in (1, 2))
    project_inserted(request_key(capped_id), request_key(denied_id))
    capped_before, denied_before = read_stored_row(request_key(capped_id)), read_stored_row(request_key(denied_id))
    graph.record_ask_capped(capped_id)
    send_action(handler, "ann", {"action": "answer", "answer": "no", "requestId": denied_id})
    project(stream_record_for_row(request_key(capped_id), capped_before), stream_record_for_row(request_key(denied_id), denied_before))

    log_rows = read_org_log_rows()
    assert sorted((row["requestId"], row["event"]) for row in log_rows) == sorted([
        (capped_id, "message_held"), (capped_id, "ask_capped"), (denied_id, "message_held"), (denied_id, "contact_confirmed_not_me")])
    for log_row in log_rows:
        assert log_row["event"] in {str(event) for event in OrgEvent}
        has_channel = log_row["event"] in CHANNEL_BY_EVENT  # ask_capped has none
        assert set(log_row) == ORG_LOG_REQUEST_EVENT_FIELDS | ({"channel"} if has_channel else set())
        assert not has_channel or log_row["channel"] == CHANNEL_BY_EVENT[log_row["event"]]
        assert (log_row["memberRef"], log_row["sourceKey"]) == (GRANDMA_ADDRESS, f"REQUEST#{log_row['requestId']}")
    stored_log_text = json.dumps(log_rows, default=str)
    assert not [text for text in OFFICE_EMAIL_TEXTS if text in stored_log_text]

    pushed_messages.clear()
    send_action(handler, "ann", {"action": "sync"})
    dashboard = pushed_messages[-1]["org"]
    assert set(dashboard) == DASHBOARD_FIELDS
    assert dashboard["members"] == [{"memberRef": GRANDMA_ADDRESS, "residentName": "Grandma", "tags": [], "state": "confirmed_not_me",
                                     "shares": "alerts_only", "lastEventAt": max(row["at"] for row in log_rows)}]
    assert all(set(actionable) == ACTIONABLE_FIELDS for actionable in dashboard["actionable"])
    actionable_by_source = {actionable["sourceKey"]: actionable for actionable in dashboard["actionable"]}
    assert {source_key: actionable["state"] for source_key, actionable in actionable_by_source.items()} == {
        f"REQUEST#{capped_id}": "held_no_answer", f"REQUEST#{denied_id}": "confirmed_not_me"}
    # the capped episode's newest event, ask_capped, is timed at the request's createdAt
    assert actionable_by_source[f"REQUEST#{capped_id}"]["at"] == graph.get_request(capped_id)["createdAt"]
    assert set(dashboard["coverage"]) == {"enrolled", "withVerifiedContact", "visibilityChosen", "seenInLastSevenDays"}
    assert [set(entry) for entry in dashboard["callList"]] == [{"memberRef", "residentName"}]


def test_zero_events_reads_nothing_open(sunrise):
    handler, _pushed_messages = sunrise
    share_with_the_office(handler)
    now = datetime.now(timezone.utc)
    dashboard = sunrise_dashboard(now)
    assert dashboard["members"] == [{"memberRef": GRANDMA_ADDRESS, "residentName": "Grandma", "tags": [], "state": "nothing_open",
                                     "shares": "alerts_only", "lastEventAt": None}]
    assert dashboard["actionable"] == []
    assert dashboard["coverage"] == {"enrolled": 1, "withVerifiedContact": 0, "visibilityChosen": 1, "seenInLastSevenDays": 0}
    assert dashboard["callList"] == [{"memberRef": GRANDMA_ADDRESS, "residentName": "Grandma"}]
    graph.register_connection("conn-grandma-1", "grandma")  # Grandma opens her PWA
    project_inserted({"PK": "USER#grandma", "SK": "CONN#conn-grandma-1"})
    dashboard = sunrise_dashboard(now)
    assert dashboard["members"][0]["state"] == "nothing_open" and dashboard["callList"] == []
    assert dashboard["coverage"]["seenInLastSevenDays"] == 1


def test_stale_canary_turns_dashboard_amber(sunrise):
    canary_time = datetime.now(timezone.utc).replace(microsecond=0)
    never_stamped = sunrise_dashboard(canary_time)
    assert (never_stamped["lastProjectedAt"], never_stamped["isStale"]) == (None, True)
    # The CDK schedule's Universal putItem target is the only writer of this row (no Python writer): its Item, put here.
    graph.get_table().put_item(Item={**CANARY_KEY, "writtenAt": graph.iso_timestamp(canary_time)})
    project(stream_record_for_row(CANARY_KEY, None, created_at=canary_time))
    fresh = sunrise_dashboard(canary_time)
    assert (fresh["lastProjectedAt"], fresh["isStale"]) == (graph.iso_timestamp(canary_time), False)
    assert sunrise_dashboard(canary_time + timedelta(minutes=16))["isStale"] is True


def test_absent_visibility_reads_not_shared(sunrise):
    handler, _pushed_messages = sunrise
    # Grandma chose alerts_only before the projection started (it reads from LATEST): her ORGPREF# row lets events through,
    # but no coverage fact was ever projected onto her MEMBER# row.
    send_action(handler, "grandma", {"action": "set_org_visibility", "visibility": "alerts_only"})
    request_id = new_office_request("inbox/office-1.eml", add_office_to_circle(GRANDMA_ADDRESS, SUNRISE_ORG_ID))
    project_inserted(request_key(request_id))
    assert [row["event"] for row in read_org_log_rows()] == ["message_held"]
    dashboard = sunrise_dashboard(datetime.now(timezone.utc))
    assert (dashboard["members"][0]["state"], dashboard["members"][0]["shares"], dashboard["members"][0]["lastEventAt"]) == (
        "not_shared", "none", None)  # her logged message_held is not shown while no coverage fact says she shares
    assert (dashboard["actionable"], dashboard["coverage"]["visibilityChosen"]) == ([], 0)
    preference_before = read_stored_row(SUNRISE_PREFERENCE_KEY)
    send_action(handler, "grandma", {"action": "set_org_visibility", "visibility": "alerts_only"})  # the migration re-pick
    project(stream_record_for_row(SUNRISE_PREFERENCE_KEY, preference_before))
    assert sunrise_dashboard(datetime.now(timezone.utc))["members"][0]["state"] == "held"


def projected(event: OrgEvent, minute: int) -> ProjectedEvent:
    """One event of the same episode at 10:<minute>; only the event and its time drive the fold."""
    return ProjectedEvent(org_id=SUNRISE_ORG_ID, member_ref=GRANDMA_ADDRESS, event=event, source_key=EPISODE_SOURCE_KEY,
                          at=f"2026-10-28T10:{minute:02d}:00Z", channel=None,
                          request_id=EPISODE_SOURCE_KEY.removeprefix("REQUEST#"), case_id=None, ttl_epoch_seconds=0)


# spec.md:589, state table enter -> exit: each open state, the events that enter it, and where each exit event leads.
EXITS_BY_STATE = {
    MemberSafetyState.HELD: ((OrgEvent.MESSAGE_HELD,), {
        OrgEvent.CONTACT_CONFIRMED_YES: None, OrgEvent.CONTACT_CONFIRMED_NOT_ME: MemberSafetyState.CONFIRMED_NOT_ME,
        OrgEvent.ASK_TIMED_OUT: MemberSafetyState.HELD_NO_ANSWER, OrgEvent.ASK_CAPPED: MemberSafetyState.HELD_NO_ANSWER}),
    MemberSafetyState.HELD_NO_ANSWER: ((OrgEvent.MESSAGE_HELD, OrgEvent.ASK_TIMED_OUT), {
        OrgEvent.CONTACT_CONFIRMED_YES: None, OrgEvent.CONTACT_CONFIRMED_NOT_ME: MemberSafetyState.CONFIRMED_NOT_ME,
        OrgEvent.ORG_RESOLVED: None}),
    MemberSafetyState.CONTACT_UNREACHABLE: ((OrgEvent.MESSAGE_HELD, OrgEvent.CONTACT_UNREACHABLE), {
        OrgEvent.CONTACT_CONFIRMED_YES: None, OrgEvent.CONTACT_CONFIRMED_NOT_ME: MemberSafetyState.CONFIRMED_NOT_ME,
        OrgEvent.ORG_RESOLVED: None}),
    MemberSafetyState.CONFIRMED_NOT_ME: ((OrgEvent.MESSAGE_HELD, OrgEvent.CONTACT_CONFIRMED_NOT_ME), {
        OrgEvent.ANSWER_REVERSED: None, OrgEvent.ORG_RESOLVED: None}),
    MemberSafetyState.WARNED: ((OrgEvent.PAYMENT_DOUBTED,), {OrgEvent.ORG_RESOLVED: None}),
    MemberSafetyState.CASE_OPEN: ((OrgEvent.WRONG_PAYMENT_REPORTED,), {OrgEvent.CASE_CLOSED: None}),
}


def test_every_state_has_an_exit_event():
    assert set(EXITS_BY_STATE) == set(MemberSafetyState) - {MemberSafetyState.NOT_SHARED, MemberSafetyState.NOTHING_OPEN}
    for state, (entry_events, exits) in EXITS_BY_STATE.items():
        entered = [projected(event, minute) for minute, event in enumerate(entry_events)]
        assert episode_state(entered) is state
        assert None in exits.values()  # some exit leaves nothing open
        for exit_event, state_after in exits.items():
            assert episode_state([*entered, projected(exit_event, 30)]) is state_after, (state, exit_event)
    same_time = [projected(OrgEvent.ASK_CAPPED, 0), projected(OrgEvent.MESSAGE_HELD, 0)]  # folded in OrgEvent order
    assert episode_state(same_time) is MemberSafetyState.HELD_NO_ANSWER


def test_unreachable_before_held_reads_contact_unreachable():
    """contact_unreachable is timed when its record was written, which may precede message_held: both orders fold alike."""
    unreachable_first = [projected(OrgEvent.CONTACT_UNREACHABLE, 0), projected(OrgEvent.MESSAGE_HELD, 1)]
    held_first = [projected(OrgEvent.MESSAGE_HELD, 0), projected(OrgEvent.CONTACT_UNREACHABLE, 1)]
    assert [episode_state(unreachable_first), episode_state(held_first)] == [MemberSafetyState.CONTACT_UNREACHABLE] * 2


def test_reopened_episode_can_be_resolved_again(sunrise, monkeypatch):
    handler, pushed_messages = sunrise
    share_with_the_office(handler)
    request_id = new_office_request("inbox/office-1.eml", add_office_to_circle(GRANDMA_ADDRESS, SUNRISE_ORG_ID))
    source_key = f"REQUEST#{request_id}"
    project_inserted(request_key(request_id))
    project(time_out(request_id, monkeypatch))
    resolve_body = {"action": "org_resolve", "memberRef": GRANDMA_ADDRESS, "sourceKey": source_key}
    send_action(org_actions_handler, "ann", resolve_body)
    [first_resolve_row] = read_resolve_rows(source_key)
    project_inserted(key_of(first_resolve_row))
    assert sunrise_dashboard(datetime.now(timezone.utc))["actionable"] == []

    # Every writer stamps graph.now_timestamp(): the office's No comes a day later, the second resolve a minute after it.
    a_day_later = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
    monkeypatch.setattr(graph, "now_timestamp", lambda: graph.iso_timestamp(a_day_later))
    request_before = read_stored_row(request_key(request_id))
    send_action(handler, "ann", {"action": "answer", "answer": "no", "requestId": request_id})
    project(stream_record_for_row(request_key(request_id), request_before))
    reopened = sunrise_dashboard(a_day_later)["actionable"]
    assert [(actionable["sourceKey"], actionable["state"]) for actionable in reopened] == [(source_key, "confirmed_not_me")]

    second_resolved_at = graph.iso_timestamp(a_day_later + timedelta(minutes=1))
    monkeypatch.setattr(graph, "now_timestamp", lambda: second_resolved_at)
    pushed_messages.clear()
    send_action(org_actions_handler, "ann", resolve_body)
    assert pushed_messages == []  # recorded: success sends no reply
    second_resolve_ttl = int((a_day_later + timedelta(minutes=1)).timestamp()) + RESOLVE_ROW_TTL_SECONDS
    assert read_resolve_rows(source_key) == [first_resolve_row, {
        "PK": f"ORG#{SUNRISE_ORG_ID}", "SK": f"RESOLVE#{source_key}#{second_resolved_at}", "memberRef": GRANDMA_ADDRESS,
        "adminId": "ann", "at": second_resolved_at, "sourceKey": source_key, "ttlEpochSeconds": second_resolve_ttl}]
    project_inserted(key_of(read_resolve_rows(source_key)[1]))
    assert sunrise_dashboard(a_day_later)["actionable"] == []
    resolved_log_rows = [row for row in read_org_log_rows() if row["event"] == "org_resolved"]
    assert [(row["sourceKey"], row["requestId"]) for row in resolved_log_rows] == [(source_key, request_id)] * 2
    assert resolved_log_rows[1]["ttlEpochSeconds"] == second_resolve_ttl


def test_only_a_failed_resolve_condition_is_not_applied(sunrise, monkeypatch):
    handler, pushed_messages = sunrise
    share_with_the_office(handler)
    request_id = new_office_request("inbox/office-1.eml", add_office_to_circle(GRANDMA_ADDRESS, SUNRISE_ORG_ID))
    project_inserted(request_key(request_id))
    request_before = read_stored_row(request_key(request_id))
    send_action(handler, "ann", {"action": "answer", "answer": "no", "requestId": request_id})
    project(stream_record_for_row(request_key(request_id), request_before))  # Not me: an episode staff may resolve
    source_key = f"REQUEST#{request_id}"
    resolve_body = {"action": "org_resolve", "memberRef": GRANDMA_ADDRESS, "sourceKey": source_key}
    same_second = graph.now_timestamp()
    monkeypatch.setattr(graph, "now_timestamp", lambda: same_second)
    send_action(org_actions_handler, "amy", resolve_body)
    pushed_messages.clear()
    send_action(org_actions_handler, "ann", resolve_body)  # the same RESOLVE# sort key: its own condition fails
    assert pushed_messages == [rejection(None, "not_applied")]
    [resolve_row] = read_resolve_rows(source_key)
    assert resolve_row["adminId"] == "amy"

    client = graph.get_table().meta.client

    def transact_cancelled_by_a_conflict(**_arguments):
        raise ClientError(TRANSACTION_ERRORS["cancelled_by_a_conflict"], "TransactWriteItems")

    monkeypatch.setattr(client, "transact_write_items", transact_cancelled_by_a_conflict)
    pushed_messages.clear()
    with pytest.raises(ClientError):
        send_action(org_actions_handler, "ann", resolve_body)
    assert pushed_messages == []  # a conflict raises (the invocation fails): never a not_applied reply
    assert read_resolve_rows(source_key) == [resolve_row]
