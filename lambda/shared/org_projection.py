"""The org projection's writes: the only writer of ORGLOG#<orgId> rows, ORGSTAT#<orgId> day counts and the MEMBER# coverage
fact (coverageVisibility, hasVerifiedContact, lastSeenDay), and the only visibility filter. An event reaches ORGLOG# only when
the resident's own ORGPREF# choice passes it (org_events.passes_visibility; no choice reads as NONE). Runs in
pidge_org_projection, whose role may write ORG#*, ORGLOG#* and ORGSTAT#* only."""

from datetime import datetime, timedelta, timezone

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from shared import circle, graph, org_events, orgs, profiles
from shared.enums import UserRole, VerifiedVia
from shared.org_enums import OrgEvent, OrgVisibility
from shared.org_events import ORG_LOG_PARTITION_PREFIX, ORG_STATS_DAY_SORT_KEY_PREFIX, ORG_STATS_PARTITION_PREFIX, ProjectedEvent
from shared.org_ids import is_org_contact_sort_key
from shared.org_preferences import ORG_PREFERENCE_SORT_KEY_PREFIX, read_org_visibility
from shared.profiles import ReceiverProfile
from shared.reach_points import verified_via_of
from shared.sources import query_all_pages

ORG_STATS_RETENTION = timedelta(days=400)


def log_row_for(projected_event: ProjectedEvent) -> dict:
    """ORGLOG#<orgId>/<sourceKey>#<event>#<at>: the member, the event, the episode, the time, the channel when the event has
    one, the request or case id, and the source row's ttl. Never a subject, summary, sender, contact name or amount."""
    log_row = {
        "PK": f"{ORG_LOG_PARTITION_PREFIX}{projected_event.org_id}",
        "SK": f"{projected_event.source_key}#{projected_event.event}#{projected_event.at}",
        "memberRef": projected_event.member_ref, "event": str(projected_event.event), "sourceKey": projected_event.source_key,
        "at": projected_event.at, "ttlEpochSeconds": projected_event.ttl_epoch_seconds,
    }
    if projected_event.channel is not None:
        log_row["channel"] = str(projected_event.channel)
    if projected_event.request_id is not None:
        log_row["requestId"] = projected_event.request_id
    if projected_event.case_id is not None:
        log_row["caseId"] = projected_event.case_id
    return log_row


def day_stats_key(org_id: str, day: str) -> dict:
    return {"PK": f"{ORG_STATS_PARTITION_PREFIX}{org_id}", "SK": f"{ORG_STATS_DAY_SORT_KEY_PREFIX}{day}"}


def day_stats_expiry(day: str) -> int:
    """ORGSTAT#/DAY#<day> is kept ORG_STATS_RETENTION from the start of its day (UTC)."""
    return int((datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc) + ORG_STATS_RETENTION).timestamp())


def write_projected_event(projected_event: ProjectedEvent) -> bool:
    """In ONE transaction, Puts the ORGLOG# row (only when absent) and ADDs one to the event's count on
    ORGSTAT#<orgId>/DAY#<date of at>; True. False, nothing written, when the resident's stored visibility does not pass the
    event or the row is already there (the count is not added twice). Any other failure raises."""
    visibility = read_org_visibility(projected_event.member_ref, projected_event.org_id)
    if not org_events.passes_visibility(projected_event.event, visibility):
        return False
    day = projected_event.at[:10]
    table = graph.get_table()
    try:
        table.meta.client.transact_write_items(TransactItems=[
            {"Put": {"TableName": table.name, "Item": log_row_for(projected_event), "ConditionExpression": "attribute_not_exists(SK)"}},
            {"Update": {"TableName": table.name, "Key": day_stats_key(projected_event.org_id, day),
                        "UpdateExpression": "SET ttlEpochSeconds = :expiry ADD #event :one",
                        "ExpressionAttributeNames": {"#event": str(projected_event.event)},
                        "ExpressionAttributeValues": {":expiry": day_stats_expiry(day), ":one": 1}}},
        ])
    except ClientError as error:
        if circle.is_first_item_condition_failed(error):  # the ORGLOG# Put found its row already there: a replayed record
            return False
        raise
    return True


def stamp_last_projected_at(org_id: str, projected_at: datetime) -> None:
    """SETs lastProjectedAt (and the day row's ttl) on ORGSTAT#<orgId>/DAY#<that date> unless the row already holds a newer
    or equal stamp, which leaves it as it is."""
    stamp = graph.iso_timestamp(projected_at)
    day = stamp[:10]
    try:
        graph.get_table().update_item(
            Key=day_stats_key(org_id, day), UpdateExpression="SET lastProjectedAt = :stamp, ttlEpochSeconds = :expiry",
            ConditionExpression="attribute_not_exists(lastProjectedAt) OR lastProjectedAt < :stamp",
            ExpressionAttributeValues={":stamp": stamp, ":expiry": day_stats_expiry(day)})
    except ClientError as error:
        if error.response["Error"]["Code"] != graph.CONDITIONAL_CHECK_FAILED:
            raise


def member_key(member: ReceiverProfile) -> dict:
    return {"PK": orgs.org_partition(member.org_id), "SK": f"{orgs.MEMBER_SORT_KEY_PREFIX}{member.receiver_address}"}


def has_verified_contact(receiver_address: str) -> bool:
    """Whether the circle holds a contact that is not the home's office and is no longer PENDING."""
    return any(not is_org_contact_sort_key(contact_row["SK"]) and verified_via_of(contact_row) is not VerifiedVia.PENDING
               for contact_row in graph.list_contact_rows(receiver_address))


def write_coverage_fact(member: ReceiverProfile, visibility: OrgVisibility) -> None:
    """SETs the member's full coverage fact on ORG#<orgId>/MEMBER#<address>: the visibility the resident chose and whether a
    non-office contact is verified. An UpdateItem, never a Put: tags and lastSeenDay survive, and a member the seed never
    listed gets a row."""
    graph.get_table().update_item(
        Key=member_key(member), UpdateExpression="SET coverageVisibility = :visibility, hasVerifiedContact = :hasVerifiedContact",
        ExpressionAttributeValues={":visibility": str(visibility),
                                   ":hasVerifiedContact": has_verified_contact(member.receiver_address)})


def write_verified_contact_fact(member: ReceiverProfile) -> None:
    graph.get_table().update_item(
        Key=member_key(member), UpdateExpression="SET hasVerifiedContact = :hasVerifiedContact",
        ExpressionAttributeValues={":hasVerifiedContact": has_verified_contact(member.receiver_address)})


def write_last_seen_day(member: ReceiverProfile, seen_day: str) -> bool:
    """SETs lastSeenDay "YYYY-MM-DD"; False, nothing written, when the row already holds that day or a later one."""
    try:
        graph.get_table().update_item(
            Key=member_key(member), UpdateExpression="SET lastSeenDay = :seenDay",
            ConditionExpression="attribute_not_exists(lastSeenDay) OR lastSeenDay < :seenDay",
            ExpressionAttributeValues={":seenDay": seen_day})
    except ClientError as error:
        if error.response["Error"]["Code"] == graph.CONDITIONAL_CHECK_FAILED:
            return False
        raise
    return True


def delete_member_rows_not_passing(org_id: str, member_ref: str, visibility: OrgVisibility) -> int:
    """Deletes the member's ORGLOG#<orgId> rows whose event the new visibility does not pass (NONE: all but
    ANY_VISIBILITY_EVENTS; ALERTS_ONLY: the CASE_EVENTS rows), every page read; returns how many."""
    log_rows = query_all_pages(KeyConditionExpression=Key("PK").eq(f"{ORG_LOG_PARTITION_PREFIX}{org_id}"),
                               FilterExpression=Attr("memberRef").eq(member_ref))
    rows_not_passing = [log_row for log_row in log_rows if not org_events.passes_visibility(OrgEvent(log_row["event"]), visibility)]
    with graph.get_table().batch_writer() as batch:
        for log_row in rows_not_passing:
            batch.delete_item(Key={"PK": log_row["PK"], "SK": log_row["SK"]})
    return len(rows_not_passing)


def require_member_of_org(receiver_address: str, org_id: str) -> ReceiverProfile:
    """The resident behind the address; raises UnknownReceiverError without a profile and ValueError when the resident
    belongs to another org (a row naming the wrong org is reported, never projected)."""
    member = profiles.require_receiver_profile(receiver_address)
    if member.org_id != org_id:
        raise ValueError("an ORGPREF# row names another org than the resident's")
    return member


def project_visibility_choice(keys: dict, preference_row: dict) -> str:
    """RECEIVER#<address>/ORGPREF#<orgId> written: the member's full coverage fact, then the deletions the new visibility
    calls for. Returns the org id."""
    org_id = keys["SK"].removeprefix(ORG_PREFERENCE_SORT_KEY_PREFIX)
    member = require_member_of_org(keys["PK"].removeprefix("RECEIVER#"), org_id)
    visibility = OrgVisibility(preference_row["orgVisibility"])
    write_coverage_fact(member, visibility)
    delete_member_rows_not_passing(org_id, member.receiver_address, visibility)
    return org_id


def project_contact_change(keys: dict) -> str | None:
    """RECEIVER#<address>/CONTACT#<id> written: hasVerifiedContact recomputed. The office's own row changes nothing (None)."""
    if is_org_contact_sort_key(keys["SK"]):
        return None
    member = profiles.require_receiver_profile(keys["PK"].removeprefix("RECEIVER#"))
    write_verified_contact_fact(member)
    return member.org_id


def project_connection(keys: dict, connected_at: datetime) -> str | None:
    """USER#<id>/CONN#<connectionId> put: a resident's lastSeenDay (UTC date of the record); the org id when the day moved
    on, None when it already read that day. Anyone else, or a user without a profile (get_user_profile logs and counts it),
    is not a member: None. Raises ValueError when the resident's RECEIVER# profile names another user."""
    user_id = keys["PK"].removeprefix("USER#")
    user_profile = profiles.get_user_profile(user_id)
    if user_profile is None or user_profile.role is not UserRole.RECEIVER:
        return None
    member = profiles.require_receiver_profile(user_profile.receiver_email)
    if member.receiver_user_id != user_id:
        raise ValueError("a resident's RECEIVER# profile names another user")
    if not write_last_seen_day(member, graph.iso_timestamp(connected_at)[:10]):
        return None
    return member.org_id
