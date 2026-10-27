"""DynamoDB Streams -> the org safety log (pidge-org-projection; its role writes ORG#*, ORGLOG#* and ORGSTAT#* only). Records
are projected in order; the first that raises is reported as the batch item failure by its SequenceNumber and the records
after it are left untouched (Lambda retries from that record, bisects, then sends it to OrgProjectionDeadLetterQueue). After
the batch, each org whose member rows were written gets one {"type": "org_changed"} push with no data (the admin PWA
re-syncs). Logs ids and key prefixes only, never a member's address."""

import logging

from botocore.exceptions import ClientError

from shared import channels, org_events, org_projection
from shared.enums import PushMessageType
from shared.org_enums import ProjectedRecordKind, StreamRecordEventName
from shared.org_ids import org_user_id_for

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handle(event, context):
    written_org_ids: set[str] = set()
    batch_item_failures = []
    for record in event["Records"]:
        try:
            written_org_ids.update(project_record(record))
        except Exception as error:
            log_failed_record(record, error)
            batch_item_failures.append({"itemIdentifier": record["dynamodb"]["SequenceNumber"]})
            break
    push_org_changed(written_org_ids)
    return {"batchItemFailures": batch_item_failures}


def project_record(record: dict) -> set[str]:
    """The ids of the orgs whose member rows this record wrote. The canary stamp writes ORGSTAT# only and names none."""
    event_name = StreamRecordEventName(record["eventName"])
    if event_name is StreamRecordEventName.REMOVE:
        return set()
    keys = org_events.image_of(record, "Keys")
    kind = org_events.record_kind_of(keys)
    if kind is ProjectedRecordKind.ORG_CANARY:
        org_projection.stamp_last_projected_at(keys["PK"].removeprefix("ORG#"), org_events.record_time_of(record))
        return set()
    if kind is ProjectedRecordKind.RECEIVER_ORG_PREFERENCE:
        return {org_projection.project_visibility_choice(keys, org_events.image_of(record, "NewImage"))}
    if kind is ProjectedRecordKind.RECEIVER_CONTACT:
        return org_ids_of(org_projection.project_contact_change(keys))
    if kind is ProjectedRecordKind.USER_CONNECTION:
        is_new_connection = event_name is StreamRecordEventName.INSERT
        return org_ids_of(org_projection.project_connection(keys, org_events.record_time_of(record))) if is_new_connection else set()
    written_org_ids = set()
    for projected_event in org_events.events_for_record(record):
        if org_projection.write_projected_event(projected_event):
            written_org_ids.add(projected_event.org_id)
    return written_org_ids


def org_ids_of(written_org_id: str | None) -> set[str]:
    return set() if written_org_id is None else {written_org_id}


def key_prefix(key_value: str) -> str:
    """The key up to and with its first "#" ("RECEIVER#"), a fixed upper-case key as it is ("META"), else "?": never an id
    or an address."""
    if "#" in key_value:
        return f"{key_value.partition('#')[0]}#"
    return key_value if key_value.isupper() else "?"


def log_failed_record(record: dict, error: Exception) -> None:
    keys = record["dynamodb"]["Keys"]
    reason = error.response["Error"]["Code"] if isinstance(error, ClientError) else type(error).__name__
    logger.error("record %s (%s / %s, sequence %s) not projected: %s", record["eventID"], key_prefix(keys["PK"]["S"]),
                 key_prefix(keys["SK"]["S"]), record["dynamodb"]["SequenceNumber"], reason)


def push_org_changed(org_ids: set[str]) -> None:
    """One org_changed to every open connection of each org's admins. A gone connection is left in place: this role may not
    delete CONN#/USER# rows."""
    for org_id in sorted(org_ids):
        delivered_count = channels.push_to_user(org_user_id_for(org_id), {"type": PushMessageType.ORG_CHANGED},
                                                should_remove_gone_connections=False)
        logger.info("org_changed for org %s pushed to %d connection(s)", org_id, delivered_count)
