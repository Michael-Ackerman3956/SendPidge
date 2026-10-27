"""WebSocket route org_resolve (pidge-org, its own function and role, writes limited to ORG#*/ORGAUDIT#*): an org admin marks
one member's open episode followed up. The admin comes from the connection's user -> orgs.require_org_admin, never the body;
the body's memberRef and sourceKey are parsed, must name a MEMBER# of the admin's own org, and the episode must be in a
RESOLVABLE state. ORG#<orgId>/RESOLVE#<sourceKey> and its ORGAUDIT# row are written in one transaction; the projection turns
the RESOLVE# row into org_resolved. Only a refusal is answered. Logs ids only (never a member's address)."""

import json
import logging
import re

from botocore.exceptions import ClientError

from shared import channels, circle, graph, org_audit, org_view, orgs
from shared.enums import ActionOutcome, ClientAction
from shared.org_enums import OrgAuditAction
from shared.org_events import RESOLVABLE_STATES, RESOLVE_SORT_KEY_PREFIX
from shared.orgs import OrgAdmin

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SOURCE_KEY_PATTERN = re.compile(r"(REQUEST#req|CASE#case)-[0-9a-f-]{36}")  # used with fullmatch
HANDLED = {"statusCode": 200}
FORBIDDEN = {"statusCode": 403}


def handle(event, context):
    """Authorization first: an unregistered connection is FORBIDDEN and a caller who is not a complete org admin is
    NOT_ALLOWED before the body is read; an unparseable body or an action without an org handler is BAD_MESSAGE."""
    connection_id = event["requestContext"]["connectionId"]
    user_id = graph.get_connection_user_id(connection_id)
    if user_id is None:
        logger.warning("org action from unregistered connection %s", connection_id)
        return FORBIDDEN
    org_admin = orgs.require_org_admin(user_id)
    if org_admin is None:
        channels.reject_action(connection_id, None, ActionOutcome.NOT_ALLOWED)
        return HANDLED
    try:
        body = json.loads(event["body"])
        action = ClientAction(body["action"])
    except (ValueError, TypeError, KeyError):
        channels.reject_action(connection_id, None, ActionOutcome.BAD_MESSAGE)
        return HANDLED
    if action not in ORG_ACTION_HANDLERS:
        channels.reject_action(connection_id, None, ActionOutcome.BAD_MESSAGE)
        return HANDLED
    ORG_ACTION_HANDLERS[action](connection_id, org_admin, body)
    return HANDLED


def parse_source_key(value) -> str:
    """The sourceKey "REQUEST#req-<uuid>" or "CASE#case-<uuid>"; raises ValueError for anything else."""
    if not isinstance(value, str) or not SOURCE_KEY_PATTERN.fullmatch(value):
        raise ValueError("source key")
    return value


def is_member_of_org(org_id: str, member_ref: str) -> bool:
    response = graph.get_table().get_item(
        Key={"PK": orgs.org_partition(org_id), "SK": f"{orgs.MEMBER_SORT_KEY_PREFIX}{member_ref}"})
    return "Item" in response


def handle_org_resolve(connection_id: str, org_admin: OrgAdmin, body: dict) -> None:
    """BAD_MESSAGE for a malformed memberRef or sourceKey; NOT_ALLOWED for a member outside the admin's org; NOT_APPLIED for
    an episode of that member that is not in a RESOLVABLE state, or already resolved."""
    try:
        member_ref = circle.parse_email_address(body["memberRef"])
        source_key = parse_source_key(body["sourceKey"])
    except (KeyError, ValueError):
        channels.reject_action(connection_id, None, ActionOutcome.BAD_MESSAGE)
        return
    if not is_member_of_org(org_admin.org_id, member_ref):
        logger.info("org_resolve by %s refused: not a member of org %s", org_admin.user_id, org_admin.org_id)
        channels.reject_action(connection_id, None, ActionOutcome.NOT_ALLOWED)
        return
    if org_view.episode_state_for(org_admin.org_id, member_ref, source_key) not in RESOLVABLE_STATES:
        logger.info("org_resolve of %s by %s not applied: not resolvable", source_key, org_admin.user_id)
        channels.reject_action(connection_id, None, ActionOutcome.NOT_APPLIED)
        return
    outcome = write_resolve(org_admin, member_ref, source_key)
    logger.info("org_resolve of %s by %s in org %s: %s", source_key, org_admin.user_id, org_admin.org_id, outcome)
    if outcome is not ActionOutcome.RECORDED:
        channels.reject_action(connection_id, None, outcome)


def write_resolve(org_admin: OrgAdmin, member_ref: str, source_key: str) -> ActionOutcome:
    """ONE transaction: Put ORG#<orgId>/RESOLVE#<sourceKey> {memberRef, adminId, at} and the org_resolve audit row (targetRef
    the sourceKey), each only when absent. Cancelled (the episode was resolved before): NOT_APPLIED, nothing written."""
    table = graph.get_table()
    resolve_row = {"PK": orgs.org_partition(org_admin.org_id), "SK": f"{RESOLVE_SORT_KEY_PREFIX}{source_key}",
                   "memberRef": member_ref, "adminId": org_admin.user_id, "at": graph.now_timestamp()}
    audit_row = org_audit.build_audit_row(org_admin, OrgAuditAction.ORG_RESOLVE, source_key)
    try:
        table.meta.client.transact_write_items(TransactItems=[
            {"Put": {"TableName": table.name, "Item": resolve_row, "ConditionExpression": "attribute_not_exists(SK)"}},
            {"Put": {"TableName": table.name, "Item": audit_row, "ConditionExpression": "attribute_not_exists(SK)"}},
        ])
    except ClientError as error:
        if error.response["Error"]["Code"] == circle.TRANSACTION_CANCELLED:
            return ActionOutcome.NOT_APPLIED
        raise
    return ActionOutcome.RECORDED


ORG_ACTION_HANDLERS = {  # each acts for the caller's own OrgAdmin, resolved first by handle
    ClientAction.ORG_RESOLVE: handle_org_resolve,
}
