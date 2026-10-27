"""The org audit log: ORGAUDIT#<orgId>/TS#<iso>#<uuid4> {adminId, action, targetRef, ttlEpochSeconds}, one row per admin act
on member data (a dashboard view, an answer as the office, an org_resolve). Rows are only ever Put, under
attribute_not_exists(SK). Callers write the row before the act and stop when the write raises (fail closed)."""

import uuid

from shared import graph
from shared.org_enums import OrgAuditAction
from shared.orgs import OrgAdmin

AUDIT_PARTITION_PREFIX = "ORGAUDIT#"
AUDIT_SORT_KEY_PREFIX = "TS#"
AUDIT_RETENTION_SECONDS = 365 * 24 * 3600


def audit_partition(org_id: str) -> str:
    return f"{AUDIT_PARTITION_PREFIX}{org_id}"


def build_audit_row(org_admin: OrgAdmin, action: OrgAuditAction, target_ref: str) -> dict:
    """The row for one act by this admin in their own org: now plus a fresh uuid4 in the sort key, kept 365 days."""
    return {"PK": audit_partition(org_admin.org_id), "SK": f"{AUDIT_SORT_KEY_PREFIX}{graph.now_timestamp()}#{uuid.uuid4()}",
            "adminId": org_admin.user_id, "action": str(action), "targetRef": target_ref,
            "ttlEpochSeconds": graph.retention_epoch(AUDIT_RETENTION_SECONDS)}


def write_audit_row(org_admin: OrgAdmin, action: OrgAuditAction, target_ref: str) -> None:
    """Puts one audit row; any failure raises, so the caller sends and records nothing."""
    graph.get_table().put_item(Item=build_audit_row(org_admin, action, target_ref), ConditionExpression="attribute_not_exists(SK)")
