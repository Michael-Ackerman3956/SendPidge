"""Every admin act on member data leaves one ORGAUDIT#<orgId> row naming the admin, the act and its target, written before
anything is pushed or recorded: a failed audit write sends and records nothing (fail closed)."""

import time

import pytest
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from pidge_org import handler as org_actions_handler
from shared import graph
from shared.enums import Friction
from shared.org_audit import AUDIT_RETENTION_SECONDS, audit_partition
from tests.helpers.stream_records import read_stored_row, stream_record_for_row
from tests.test_org import (
    GRANDMA_ADDRESS, MAPLE_ORG_ID, OFFICE_VERDICT_FIELDS, SUNRISE_ORG_ID, add_office_to_circle, new_office_request, rejection,
    seed_sunrise_and_maple,
)
from tests.test_org_view import project, project_inserted, request_key, share_with_the_office, sunrise  # noqa: F401
from tests.test_verdict import aws  # noqa: F401
from tests.test_verify_contact import seed_circle_contact, send_action, verdict_handler_without_profiles  # noqa: F401

AUDIT_ROW_FIELDS = {"PK", "SK", "adminId", "action", "targetRef", "ttlEpochSeconds"}
CLOCK_SLACK_SECONDS = 60


def read_audit_rows(org_id: str) -> list[dict]:
    return graph.get_table().query(KeyConditionExpression=Key("PK").eq(audit_partition(org_id)))["Items"]


def audit_fields(audit_row: dict) -> tuple:
    return audit_row["adminId"], audit_row["action"], audit_row["targetRef"]


def refuse_audit_writes(monkeypatch) -> None:
    """The table refuses every ORGAUDIT# Put, as an outage would; every other write goes through."""
    table = graph.get_table()
    put_item = table.put_item

    def put_item_refusing_audit_rows(**arguments):
        if arguments["Item"]["PK"].startswith("ORGAUDIT#"):
            raise ClientError({"Error": {"Code": "InternalServerError", "Message": "injected"}}, "PutItem")
        return put_item(**arguments)

    monkeypatch.setattr(table, "put_item", put_item_refusing_audit_rows)


def test_member_view_writes_audit_row(verdict_handler_without_profiles, monkeypatch):  # noqa: F811
    handler, pushed_messages = verdict_handler_without_profiles
    seed_sunrise_and_maple()
    send_action(handler, "ann", {"action": "sync"})
    [sync_state] = pushed_messages
    assert (sync_state["type"], sync_state["role"]) == ("sync_state", "org_admin")
    [audit_row] = read_audit_rows(SUNRISE_ORG_ID)
    assert set(audit_row) == AUDIT_ROW_FIELDS and audit_row["SK"].startswith("TS#")
    assert audit_fields(audit_row) == ("ann", "member_view", SUNRISE_ORG_ID)
    assert abs(int(audit_row["ttlEpochSeconds"]) - (time.time() + AUDIT_RETENTION_SECONDS)) < CLOCK_SLACK_SECONDS
    assert read_audit_rows(MAPLE_ORG_ID) == []

    refuse_audit_writes(monkeypatch)
    with pytest.raises(ClientError):
        send_action(handler, "amy", {"action": "sync"})
    assert pushed_messages == [sync_state]  # no sync_state after the failed audit
    assert len(read_audit_rows(SUNRISE_ORG_ID)) == 1


def test_office_answer_writes_audit_row(verdict_handler_without_profiles, monkeypatch):  # noqa: F811
    handler, _pushed_messages = verdict_handler_without_profiles
    seed_sunrise_and_maple()
    office = add_office_to_circle(GRANDMA_ADDRESS, SUNRISE_ORG_ID)
    first_id, second_id = (new_office_request(f"inbox/office-{number}.eml", office) for number in (1, 2))
    seed_circle_contact(GRANDMA_ADDRESS, "jimin")
    jimin = next(contact for contact in graph.list_contacts(GRANDMA_ADDRESS) if contact.contact_key == "CONTACT#jimin")
    family_request_id = graph.request_id_of(
        graph.create_request("inbox/family-1.eml", GRANDMA_ADDRESS, jimin, OFFICE_VERDICT_FIELDS, Friction.SINGLE_TAP, None))
    for user_id in ("max", "grandma"):  # Maple's admin and the resident: refused by is_expected_sender, nothing audited
        send_action(handler, user_id, {"action": "answer", "answer": "no", "requestId": first_id})
    send_action(handler, "jimin", {"action": "answer", "answer": "no", "requestId": family_request_id})
    assert graph.get_request(family_request_id)["status"] == "denied"  # a family answer is recorded and never audited
    assert read_audit_rows(SUNRISE_ORG_ID) == [] and read_audit_rows(MAPLE_ORG_ID) == []

    send_action(handler, "ann", {"action": "answer", "answer": "no", "requestId": first_id})
    [audit_row] = read_audit_rows(SUNRISE_ORG_ID)
    assert audit_fields(audit_row) == ("ann", "answer", first_id) and set(audit_row) == AUDIT_ROW_FIELDS
    assert graph.get_request(first_id)["status"] == "denied"

    refuse_audit_writes(monkeypatch)
    unanswered = graph.get_request(second_id)
    with pytest.raises(ClientError):
        send_action(handler, "amy", {"action": "answer", "answer": "no", "requestId": second_id})
    assert graph.get_request(second_id) == unanswered


def test_org_resolve_writes_resolve_row_and_audit_row(sunrise):  # noqa: F811
    handler, pushed_messages = sunrise
    share_with_the_office(handler)
    request_id = new_office_request("inbox/office-1.eml", add_office_to_circle(GRANDMA_ADDRESS, SUNRISE_ORG_ID))
    project_inserted(request_key(request_id))
    request_before = read_stored_row(request_key(request_id))
    send_action(handler, "ann", {"action": "answer", "answer": "no", "requestId": request_id})
    project(stream_record_for_row(request_key(request_id), request_before))  # Not me: an episode staff may resolve
    source_key = f"REQUEST#{request_id}"
    resolve_body = {"action": "org_resolve", "memberRef": GRANDMA_ADDRESS.upper(), "sourceKey": source_key}
    pushed_messages.clear()
    send_action(org_actions_handler, "amy", resolve_body)
    assert pushed_messages == []  # success sends no reply; the projection's org_changed brings the re-sync
    resolve_key = {"PK": f"ORG#{SUNRISE_ORG_ID}", "SK": f"RESOLVE#{source_key}"}
    resolve_row = graph.get_table().get_item(Key=resolve_key)["Item"]
    assert set(resolve_row) == {"PK", "SK", "memberRef", "adminId", "at"}
    assert (resolve_row["memberRef"], resolve_row["adminId"]) == (GRANDMA_ADDRESS, "amy")
    [resolve_audit_row] = [row for row in read_audit_rows(SUNRISE_ORG_ID) if row["action"] == "org_resolve"]
    assert audit_fields(resolve_audit_row) == ("amy", "org_resolve", source_key) and set(resolve_audit_row) == AUDIT_ROW_FIELDS

    send_action(org_actions_handler, "ann", resolve_body)
    assert pushed_messages == [rejection(None, "not_applied")]
    assert graph.get_table().get_item(Key=resolve_key)["Item"] == resolve_row
    assert [row["action"] for row in read_audit_rows(SUNRISE_ORG_ID)].count("org_resolve") == 1
