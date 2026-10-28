"""The org projection (pidge_org_projection.handler.handle) on stream records built from rows the production writers put on
moto: only the resident's own visibility lets an event into ORGLOG#, a replayed record writes once, an episode folds to its
state, coverage facts land on MEMBER# without a log row, and a failed record is the batch item failure with the records after
it untouched."""

import importlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from shared import channels, circle, graph, inbox_rows, org_preferences, org_view, orgs, profiles, verdict
from shared.enums import (
    ActionOutcome, Channel, Friction, Jurisdiction, PaymentRail, PolicyDecision, VerdictAnswer, VerifiedVia, WarnReason,
)
from shared.org_enums import MemberSafetyState, OrgEvent, OrgVisibility
from shared.org_events import passes_visibility
from tests.helpers.profiles import seed_resident
from tests.helpers.stream_records import read_stored_row, stream_event, stream_record_for_row
from tests.test_org import GRANDMA_ADDRESS, SUNRISE_ORG_ID, RecordingManagementClient, seed_sunrise_and_maple
from tests.test_verdict import JIMIN, VERDICT_FIELDS, at, aws, seed_contact  # noqa: F401
from tests.test_verify_contact import MINSOO_FORM, send_action

NO_FAILURES = {"batchItemFailures": []}
CAROL_ADDRESS = "carol@example.com"
CONNECTED_AT = datetime(2026, 10, 28, 9, 30, tzinfo=timezone.utc)
# event -> whether it passes with visibility (absent, none, alerts_only, alerts_and_cases); absent is no ORGPREF# row at all.
VISIBILITY_GOLDEN = {
    "message_held": (False, False, True, True),
    "ask_capped": (False, False, True, True),
    "ask_timed_out": (False, False, True, True),
    "contact_confirmed_yes": (False, False, True, True),
    "contact_confirmed_not_me": (False, False, True, True),
    "answer_reversed": (False, False, True, True),
    "contact_unreachable": (False, False, True, True),
    "payment_doubted": (False, False, True, True),
    "wrong_payment_reported": (False, False, False, True),
    "recovery_pack_drafted": (False, False, False, True),
    "case_closed": (False, False, False, True),
    "contact_request": (False, False, True, True),
    "broadcast_sent": (False, False, True, True),
    "broadcast_acknowledged": (True, True, True, True),
    "help_requested": (True, True, True, True),
    "org_resolved": (True, True, True, True),
}
TRANSACTION_ERRORS = {
    "internal_server_error": {"Error": {"Code": "InternalServerError", "Message": "Internal server error"}},
    "cancelled_by_a_conflict": {"Error": {"Code": "TransactionCanceledException", "Message": "Transaction cancelled"},
                                "CancellationReasons": [{"Code": "TransactionConflict"}, {"Code": "None"}]},
}


@pytest.fixture
def projection(aws, monkeypatch):  # noqa: F811
    """pidge-org-projection with Grandma at Sunrise (admins ann and amy) and Bob at Maple; pushes go through the production
    push_to_user and are recorded by the management client."""
    seed_sunrise_and_maple()
    recorder = RecordingManagementClient()
    monkeypatch.setattr(channels, "_management_client", recorder)
    return importlib.import_module("pidge_org_projection.handler"), recorder


def project(handler, *records: dict) -> dict:
    return handler.handle(stream_event(list(records)), None)


def resident(receiver_address: str = GRANDMA_ADDRESS) -> profiles.ReceiverProfile:
    return profiles.require_receiver_profile(receiver_address)


def choose_visibility(visibility: OrgVisibility, receiver_address: str = GRANDMA_ADDRESS) -> dict:
    """The resident's own choice (org_preferences.set_org_visibility); returns the record it wrote."""
    key = org_preferences.org_preference_key(receiver_address, SUNRISE_ORG_ID)
    old_row = read_stored_row(key)
    assert org_preferences.set_org_visibility(resident(receiver_address), visibility, datetime.now(timezone.utc)) is ActionOutcome.RECORDED
    return stream_record_for_row(key, old_row)


def request_key(request_id: str) -> dict:
    return {"PK": f"REQUEST#{request_id}", "SK": graph.METADATA_SORT_KEY}


def email_key(email_id: str) -> dict:
    return {"PK": f"EMAIL#{email_id}", "SK": graph.METADATA_SORT_KEY}


def hold_message(email_id: str, receiver_address: str = GRANDMA_ADDRESS) -> tuple[str, dict]:
    """A message held and asked of Jimin (inbox_rows.claim_row, graph.create_request: the Reader's writers); (request id, the
    request's INSERT record)."""
    inbox_rows.claim_row(Channel.EMAIL, email_id, {"s3Key": email_id, "senderAddress": "scammer@lookalike.example",
                                                   "receiverEmail": receiver_address, "quarantined": False})
    jimin = graph.Contact("CONTACT#jimin", "Jimin", "jimin")
    request_id = graph.request_id_of(graph.create_request(email_id, receiver_address, jimin, VERDICT_FIELDS, Friction.SINGLE_TAP, None,
                                                          Channel.EMAIL))
    return request_id, stream_record_for_row(request_key(request_id), None)


def answer(request_id: str, verdict_answer: VerdictAnswer, message_id: str, now: str) -> dict:
    """Jimin's in-app answer through verdict.record_answer; returns the MODIFY record."""
    old_row = read_stored_row(request_key(request_id))
    assert verdict.record_answer(request_id, verdict_answer, JIMIN, message_id, now).outcome is ActionOutcome.RECORDED
    return stream_record_for_row(request_key(request_id), old_row)


def time_out(request_id: str, monkeypatch) -> dict:
    """pidge-retry's second timer marks the request timeout; returns the MODIFY record."""
    monkeypatch.setenv("RE_ASK_DELAY_SECONDS", "600")
    monkeypatch.setenv("VOICE_ENABLED", "false")
    retry_handler = importlib.reload(importlib.import_module("pidge_retry.handler"))
    old_row = read_stored_row(request_key(request_id))
    retry_handler.handle({"requestId": request_id, "attempt": 2}, None)
    return stream_record_for_row(request_key(request_id), old_row)


def held_without_answer(handler, email_id: str, monkeypatch) -> str:
    request_id, held_record = hold_message(email_id)
    assert project(handler, held_record, time_out(request_id, monkeypatch)) == NO_FAILURES
    return request_id


def report_payment(request_id: str) -> tuple[dict, dict]:
    """Grandma's "I already paid" (verdict.report_payment); (the CASE# row, its INSERT record)."""
    payment = verdict.PaymentReport(request_id, Jurisdiction.US, PaymentRail.CARD, "Chase", Decimal("500"), "USD", "2026-10-24T01:00:00Z", "")
    result = verdict.report_payment(payment, "grandma", "grandma", GRANDMA_ADDRESS)
    assert result.outcome is ActionOutcome.RECORDED
    case_row = result.email_row
    return case_row, stream_record_for_row({"PK": case_row["PK"], "SK": graph.METADATA_SORT_KEY}, None)


def org_log_rows() -> list[dict]:
    return graph.get_table().query(KeyConditionExpression=Key("PK").eq(f"ORGLOG#{SUNRISE_ORG_ID}"))["Items"]


def logged_events(source_key: str, member_ref: str = GRANDMA_ADDRESS) -> list[str]:
    return sorted(row["event"] for row in org_log_rows() if row["sourceKey"] == source_key and row["memberRef"] == member_ref)


def org_stats_rows() -> list[dict]:
    return graph.get_table().query(KeyConditionExpression=Key("PK").eq(f"ORGSTAT#{SUNRISE_ORG_ID}"))["Items"]


def member_row() -> dict:
    return read_stored_row({"PK": f"ORG#{SUNRISE_ORG_ID}", "SK": f"MEMBER#{GRANDMA_ADDRESS}"})


def episode_of(request_id: str) -> MemberSafetyState | None:
    return org_view.episode_state_for(SUNRISE_ORG_ID, GRANDMA_ADDRESS, f"REQUEST#{request_id}")


def resolve_record(request_id: str) -> dict:
    """The INSERT record of the episode's one ORG#<orgId>/RESOLVE#REQUEST#<id>#<at> row, as pidge-org wrote it."""
    [resolve_row] = graph.get_table().query(KeyConditionExpression=Key("PK").eq(f"ORG#{SUNRISE_ORG_ID}")
                                            & Key("SK").begins_with(f"RESOLVE#REQUEST#{request_id}#"))["Items"]
    return stream_record_for_row({"PK": resolve_row["PK"], "SK": resolve_row["SK"]}, None)


def actionable_now() -> list[tuple[str, str, str]]:
    dashboard = org_view.org_dashboard_for(orgs.require_org_admin("ann"), datetime.now(timezone.utc))
    return [(item["memberRef"], item["sourceKey"], item["state"]) for item in dashboard["actionable"]]


def test_absent_visibility_writes_no_log_row(projection):
    handler, recorder = projection
    graph.register_connection("conn-ann", "ann")
    _request_id, held_record = hold_message("inbox/held-1.eml")
    assert project(handler, held_record) == NO_FAILURES
    assert org_log_rows() == [] and org_stats_rows() == [] and recorder.posts == []
    choose_visibility(OrgVisibility.ALERTS_ONLY)
    assert project(handler, held_record) == NO_FAILURES  # the same record once she chose: now it passes
    assert [row["event"] for row in org_log_rows()] == ["message_held"]


def test_alerts_only_writes_no_case_event(projection):
    handler, _recorder = projection
    seed_contact()
    choose_visibility(OrgVisibility.ALERTS_ONLY)
    request_id, held_record = hold_message("inbox/paid-1.eml")
    denied_record = answer(request_id, VerdictAnswer.NO, "m1", at(1))
    case_row, case_record = report_payment(request_id)
    assert project(handler, held_record, denied_record, case_record) == NO_FAILURES
    assert logged_events(f"REQUEST#{request_id}") == ["contact_confirmed_not_me", "message_held"]
    assert logged_events(case_row["PK"]) == []
    choose_visibility(OrgVisibility.ALERTS_AND_CASES)
    assert project(handler, case_record) == NO_FAILURES
    case_log_rows = [row for row in org_log_rows() if row["sourceKey"] == case_row["PK"]]
    assert [(row["event"], row["caseId"], "requestId" in row) for row in case_log_rows] == [
        ("wrong_payment_reported", graph.case_id_of(case_row), False)]


def test_replayed_stream_record_writes_once(projection):
    handler, recorder = projection
    graph.register_connection("conn-ann", "ann")
    choose_visibility(OrgVisibility.ALERTS_ONLY)
    _request_id, held_record = hold_message("inbox/held-1.eml")
    assert project(handler, held_record) == NO_FAILURES
    assert project(handler, held_record) == NO_FAILURES
    [log_row] = org_log_rows()
    assert log_row["event"] == "message_held"
    [stats_row] = org_stats_rows()
    assert (stats_row["SK"], stats_row["message_held"]) == (f"DAY#{log_row['at'][:10]}", 1)
    assert recorder.posts == [("conn-ann", {"type": "org_changed"})]  # the replay wrote nothing, so pushed nothing


def test_timeout_record_exits_held_state(projection, monkeypatch):
    handler, _recorder = projection
    seed_contact()
    assert project(handler, choose_visibility(OrgVisibility.ALERTS_ONLY)) == NO_FAILURES
    request_id, held_record = hold_message("inbox/held-1.eml")
    assert project(handler, held_record) == NO_FAILURES
    assert episode_of(request_id) is MemberSafetyState.HELD and actionable_now() == []
    assert project(handler, time_out(request_id, monkeypatch)) == NO_FAILURES
    assert logged_events(f"REQUEST#{request_id}") == ["ask_timed_out", "message_held"]
    assert episode_of(request_id) is MemberSafetyState.HELD_NO_ANSWER
    assert actionable_now() == [(GRANDMA_ADDRESS, f"REQUEST#{request_id}", "held_no_answer")]


@pytest.mark.parametrize("error_response", TRANSACTION_ERRORS.values(), ids=TRANSACTION_ERRORS.keys())
def test_failed_transaction_reported_as_batch_item_failure(projection, monkeypatch, error_response):
    handler, _recorder = projection
    choose_visibility(OrgVisibility.ALERTS_ONLY)
    first_id, first_record = hold_message("inbox/first.eml")
    second_id, second_record = hold_message("inbox/second.eml")
    _third_id, third_record = hold_message("inbox/third.eml")
    client = graph.get_table().meta.client
    real_transact = client.transact_write_items

    def transact_failing_on_second(**arguments):
        if arguments["TransactItems"][0]["Put"]["Item"]["requestId"] == second_id:
            raise ClientError(error_response, "TransactWriteItems")
        return real_transact(**arguments)

    monkeypatch.setattr(client, "transact_write_items", transact_failing_on_second)
    assert project(handler, first_record, second_record, third_record) == {
        "batchItemFailures": [{"itemIdentifier": second_record["dynamodb"]["SequenceNumber"]}]}
    assert [row["requestId"] for row in org_log_rows()] == [first_id]  # the third record was never tried
    [stats_row] = org_stats_rows()
    assert stats_row["message_held"] == 1
    monkeypatch.setattr(client, "transact_write_items", real_transact)
    assert project(handler, second_record, third_record) == NO_FAILURES  # Lambda's retry starts at the reported record
    assert len(org_log_rows()) == 3 and org_stats_rows()[0]["message_held"] == 3


def test_coverage_fact_written_without_log_row(projection):
    handler, recorder = projection
    preference_record = choose_visibility(OrgVisibility.ALERTS_ONLY)
    enrolment = circle.enrol_contact(GRANDMA_ADDRESS, circle.parse_contact_form(MINSOO_FORM), datetime.now(timezone.utc))
    assert enrolment.outcome is ActionOutcome.RECORDED
    contact_key = {"PK": enrolment.contact_row["PK"], "SK": enrolment.contact_row["SK"]}
    pending_record = stream_record_for_row(contact_key, None)
    assert project(handler, preference_record, pending_record) == NO_FAILURES
    assert (member_row()["coverageVisibility"], member_row()["hasVerifiedContact"]) == ("alerts_only", False)
    pending_row = read_stored_row(contact_key)
    assert circle.verify_with_code(GRANDMA_ADDRESS, pending_row, enrolment.code, VerifiedVia.MOCK, datetime.now(timezone.utc), None)
    verified_record = stream_record_for_row(contact_key, pending_row)
    connection_records = []
    for user_id in ("grandma", "ann", "jimin"):  # the resident, an org admin, a user without a profile
        graph.register_connection(f"conn-{user_id}", user_id)
        connection_records.append(stream_record_for_row({"PK": f"USER#{user_id}", "SK": f"CONN#conn-{user_id}"}, None, CONNECTED_AT))
    assert project(handler, verified_record, *connection_records) == NO_FAILURES
    stored_member = member_row()
    assert (stored_member["coverageVisibility"], stored_member["hasVerifiedContact"], stored_member["lastSeenDay"]) == (
        "alerts_only", True, "2026-10-28")
    assert stored_member["tags"] == []  # the seed's field survives: every write is an UpdateItem
    assert [row["SK"] for row in orgs.list_member_rows(SUNRISE_ORG_ID)] == [f"MEMBER#{GRANDMA_ADDRESS}"]
    assert org_log_rows() == [] and org_stats_rows() == []
    assert recorder.posts == [("conn-ann", {"type": "org_changed"})]
    graph.register_connection("conn-grandma-2", "grandma")
    same_day_record = stream_record_for_row({"PK": "USER#grandma", "SK": "CONN#conn-grandma-2"}, None, CONNECTED_AT + timedelta(hours=1))
    assert project(handler, same_day_record) == NO_FAILURES
    assert recorder.posts == [("conn-ann", {"type": "org_changed"})]  # the same day again wrote nothing, so pushed nothing


def test_confirmed_pending_projects_contact_confirmed_yes(projection):
    handler, _recorder = projection
    seed_contact()
    choose_visibility(OrgVisibility.ALERTS_ONLY)
    request_id, held_record = hold_message("inbox/held-1.eml")
    yes_record = answer(request_id, VerdictAnswer.YES, "m1", at(1))
    assert project(handler, held_record, yes_record) == NO_FAILURES
    pending_release_row = read_stored_row(request_key(request_id))
    assert verdict.release_if_still_pending(request_id).outcome is ActionOutcome.RECORDED
    released_record = stream_record_for_row(request_key(request_id), pending_release_row)
    assert read_stored_row(request_key(request_id))["status"] == "confirmed"
    assert project(handler, released_record) == NO_FAILURES
    yes_rows = [row for row in org_log_rows() if row["event"] == "contact_confirmed_yes"]
    assert [(row["at"], row["channel"]) for row in yes_rows] == [(pending_release_row["resolvedAt"], "inapp")]
    assert org_stats_rows()[0]["contact_confirmed_yes"] == 1
    assert episode_of(request_id) is None


def test_help_request_passes_visibility_none(projection):
    stored_visibilities = [org_preferences.read_org_visibility(GRANDMA_ADDRESS, SUNRISE_ORG_ID)]  # no ORGPREF# row yet
    for visibility in (OrgVisibility.NONE, OrgVisibility.ALERTS_ONLY, OrgVisibility.ALERTS_AND_CASES):
        choose_visibility(visibility)
        stored_visibilities.append(org_preferences.read_org_visibility(GRANDMA_ADDRESS, SUNRISE_ORG_ID))
    assert list(VISIBILITY_GOLDEN) == [str(event) for event in OrgEvent]  # every event, in the spec's order
    for event_value, expected_passes in VISIBILITY_GOLDEN.items():
        actual_passes = tuple(passes_visibility(OrgEvent(event_value), visibility) for visibility in stored_visibilities)
        assert actual_passes == expected_passes, event_value


def test_missing_profile_is_batch_item_failure(projection):
    handler, _recorder = projection
    choose_visibility(OrgVisibility.ALERTS_ONLY)
    _stranger_request_id, stranger_record = hold_message("inbox/stranger.eml", "stranger@example.com")
    _request_id, held_record = hold_message("inbox/held-1.eml")
    assert project(handler, stranger_record, held_record) == {
        "batchItemFailures": [{"itemIdentifier": stranger_record["dynamodb"]["SequenceNumber"]}]}
    assert org_log_rows() == []


def test_resolve_row_projects_org_resolved_and_exits_state(projection, monkeypatch):
    handler, _recorder = projection
    seed_contact()
    org_handler = importlib.import_module("pidge_org.handler")
    assert project(handler, choose_visibility(OrgVisibility.ALERTS_ONLY)) == NO_FAILURES
    first_id = held_without_answer(handler, "inbox/first.eml", monkeypatch)
    second_id = held_without_answer(handler, "inbox/second.eml", monkeypatch)
    resolve_records = []
    for request_id in (first_id, second_id):  # ann resolves both through pidge-org, the RESOLVE# writer
        send_action(org_handler, "ann", {"action": "org_resolve", "memberRef": GRANDMA_ADDRESS, "sourceKey": f"REQUEST#{request_id}"})
        resolve_records.append(resolve_record(request_id))
    assert project(handler, resolve_records[0]) == NO_FAILURES
    assert logged_events(f"REQUEST#{first_id}") == ["ask_timed_out", "message_held", "org_resolved"]
    [resolved_log_row] = [row for row in org_log_rows() if row["event"] == "org_resolved"]
    # the episode comes from the row's sourceKey attribute (never the #<at> sort key), the ttl from its ttlEpochSeconds
    resolve_image = resolve_records[0]["dynamodb"]["NewImage"]
    assert (resolved_log_row["sourceKey"], resolved_log_row["requestId"]) == (f"REQUEST#{first_id}", first_id)
    assert resolved_log_row["at"] == resolve_image["at"]["S"]
    assert resolved_log_row["ttlEpochSeconds"] == int(resolve_image["ttlEpochSeconds"]["N"])
    assert episode_of(first_id) is None
    assert actionable_now() == [(GRANDMA_ADDRESS, f"REQUEST#{second_id}", "held_no_answer")]
    assert project(handler, choose_visibility(OrgVisibility.NONE)) == NO_FAILURES
    assert project(handler, resolve_records[1]) == NO_FAILURES  # visibility none still writes it
    assert logged_events(f"REQUEST#{second_id}") == ["org_resolved"]


def test_visibility_none_deletes_the_members_rows(projection):
    handler, _recorder = projection
    seed_contact()
    seed_resident(CAROL_ADDRESS, "carol", "Carol", org_id=SUNRISE_ORG_ID)
    choose_visibility(OrgVisibility.ALERTS_ONLY, CAROL_ADDRESS)
    assert project(handler, choose_visibility(OrgVisibility.ALERTS_AND_CASES)) == NO_FAILURES
    request_id, held_record = hold_message("inbox/paid-1.eml")
    denied_record = answer(request_id, VerdictAnswer.NO, "m1", at(1))
    case_row, case_record = report_payment(request_id)
    carol_request_id, carol_record = hold_message("inbox/carol-1.eml", CAROL_ADDRESS)
    assert project(handler, held_record, denied_record, case_record, carol_record) == NO_FAILURES
    assert logged_events(f"REQUEST#{request_id}") == ["contact_confirmed_not_me", "message_held"]
    assert logged_events(case_row["PK"]) == ["wrong_payment_reported"]
    assert project(handler, choose_visibility(OrgVisibility.ALERTS_ONLY)) == NO_FAILURES
    assert logged_events(case_row["PK"]) == [] and len(logged_events(f"REQUEST#{request_id}")) == 2
    assert project(handler, choose_visibility(OrgVisibility.NONE)) == NO_FAILURES
    assert [row["memberRef"] for row in org_log_rows()] == [CAROL_ADDRESS]  # Carol's row stays
    assert logged_events(f"REQUEST#{carol_request_id}", CAROL_ADDRESS) == ["message_held"]
    assert member_row()["coverageVisibility"] == "none"


def test_ask_capped_record_reads_held_no_answer(projection):
    handler, _recorder = projection
    choose_visibility(OrgVisibility.ALERTS_ONLY)
    request_id, held_record = hold_message("inbox/held-1.eml")
    uncapped_row = read_stored_row(request_key(request_id))
    graph.record_ask_capped(request_id)  # the Reader's writer when the contact's hourly asks are used up
    capped_record = stream_record_for_row(request_key(request_id), uncapped_row)
    assert project(handler, held_record, capped_record) == NO_FAILURES
    assert logged_events(f"REQUEST#{request_id}") == ["ask_capped", "message_held"]
    [capped_log_row] = [row for row in org_log_rows() if row["event"] == "ask_capped"]
    assert capped_log_row["at"] == uncapped_row["createdAt"] and "channel" not in capped_log_row
    assert org_stats_rows()[0]["message_held"] == 1  # the capped MODIFY replayed message_held
    assert episode_of(request_id) is MemberSafetyState.HELD_NO_ANSWER  # same at: message_held folds first


def test_canary_stamps_last_projected_at_and_pushes_nothing(projection):
    handler, recorder = projection
    graph.register_connection("conn-ann", "ann")
    canary_key = {"PK": f"ORG#{SUNRISE_ORG_ID}", "SK": "CANARY"}
    stamped_at = datetime(2026, 10, 28, 9, 30, tzinfo=timezone.utc)
    records = []
    for written_at in (stamped_at, stamped_at - timedelta(minutes=5)):  # the newer stamp arrives first
        old_row = read_stored_row(canary_key)
        # The Item the scheduler's Universal putItem target writes (infra/lib/org-functions.ts); no Python code writes it.
        graph.get_table().put_item(Item={**canary_key, "writtenAt": graph.iso_timestamp(written_at)})
        records.append(stream_record_for_row(canary_key, old_row, written_at))
    assert project(handler, *records) == NO_FAILURES
    [stats_row] = org_stats_rows()
    assert (stats_row["SK"], stats_row["lastProjectedAt"]) == ("DAY#2026-10-28", "2026-10-28T09:30:00Z")
    assert stats_row["ttlEpochSeconds"] == 1827705600  # 2026-10-28T00:00:00Z plus 400 days
    assert recorder.posts == [] and org_log_rows() == []


def test_no_then_reverse_then_no_reads_not_me(projection):
    handler, _recorder = projection
    seed_contact()
    choose_visibility(OrgVisibility.ALERTS_ONLY)
    request_id, held_record = hold_message("inbox/held-1.eml")
    records = [held_record, answer(request_id, VerdictAnswer.NO, "m1", at(1)), answer(request_id, VerdictAnswer.REVERSE, "m2", at(2)),
               answer(request_id, VerdictAnswer.NO, "m3", at(3))]
    assert project(handler, *records) == NO_FAILURES
    assert logged_events(f"REQUEST#{request_id}") == ["answer_reversed", "contact_confirmed_not_me", "contact_confirmed_not_me",
                                                      "message_held"]
    assert episode_of(request_id) is MemberSafetyState.CONFIRMED_NOT_ME


def test_pass_email_with_null_fields_is_skipped(projection):
    handler, _recorder = projection
    choose_visibility(OrgVisibility.ALERTS_ONLY)
    inbox_rows.claim_row(Channel.EMAIL, "inbox/pass-1.eml", {"s3Key": "inbox/pass-1.eml", "receiverEmail": GRANDMA_ADDRESS, "quarantined": False})
    claim_record = stream_record_for_row(email_key("inbox/pass-1.eml"), None)  # no state yet
    claimed_row = read_stored_row(email_key("inbox/pass-1.eml"))
    inbox_rows.record_outcome(Channel.EMAIL, "inbox/pass-1.eml", PolicyDecision.PASS, None, (), None)
    pass_record = stream_record_for_row(email_key("inbox/pass-1.eml"), claimed_row)
    assert pass_record["dynamodb"]["NewImage"]["warnReason"] == {"NULL": True}  # present, as NULL
    assert project(handler, claim_record, pass_record) == NO_FAILURES and org_log_rows() == []
    # The same path projects an unreachable warning once, timed when it was written (receivedAt is earlier than createdAt).
    request_id, held_record = hold_message("inbox/held-1.eml")
    hold_at, warn_at, quarantine_at = (datetime.now(timezone.utc) + timedelta(seconds=seconds) for seconds in (5, 10, 15))
    email_records = []
    for write, written_at in ((lambda: inbox_rows.record_outcome(Channel.EMAIL, "inbox/held-1.eml", PolicyDecision.HOLD, None, (),
                                                                 request_id), hold_at),
                              (lambda: inbox_rows.record_outcome(Channel.EMAIL, "inbox/held-1.eml", PolicyDecision.WARN,
                                                                 WarnReason.UNREACHABLE, (), request_id), warn_at),
                              (lambda: inbox_rows.mark_quarantined(Channel.EMAIL, "inbox/held-1.eml"), quarantine_at)):
        old_row = read_stored_row(email_key("inbox/held-1.eml"))
        write()
        email_records.append(stream_record_for_row(email_key("inbox/held-1.eml"), old_row, written_at))
    assert project(handler, held_record, *email_records) == NO_FAILURES
    unreachable_rows = [row for row in org_log_rows() if row["event"] == "contact_unreachable"]
    assert [(row["sourceKey"], row["at"]) for row in unreachable_rows] == [(f"REQUEST#{request_id}", graph.iso_timestamp(warn_at))]
    assert episode_of(request_id) is MemberSafetyState.CONTACT_UNREACHABLE
