"""WebSocket route org_broadcast_send (pidge-org): a sender admin's in-app safety alert to every member of their own org. The
OrgAdmin comes from handler.handle (orgs.require_org_admin, never the body); this checks the role and the passkey link
first, then the text, then takes the org's hourly slot, then writes every row before any push. Only a refusal is answered;
a sent alert reaches each member's sockets as org_notice and every admin's as org_changed (the report re-syncs). Logs ids
and counts only, never the alert text or a member's address."""

import logging
import os

from shared import channels, org_broadcasts, orgs, profiles
from shared.enums import ActionOutcome, PushMessageType
from shared.org_enums import AdminRole
from shared.org_ids import org_user_id_for
from shared.orgs import OrgAdmin

logger = logging.getLogger(__name__)
BROADCASTS_PER_ORG_PER_HOUR_ENVIRONMENT_NAME = "BROADCASTS_PER_ORG_PER_HOUR"


def handle_org_broadcast_send(connection_id: str, org_admin: OrgAdmin, body: dict) -> None:
    """NOT_ALLOWED for a viewer, or an admin not linked to Cognito (scripts/seed_auth_users.py: a linked admin connects only
    with a passkey sign-in, never a token link); BAD_MESSAGE for text that is not 1 to ALERT_TEXT_MAX_CHARACTERS characters;
    TEXT_REFUSED for a link, address, handle, long number or a character outside the allow-list; OVER_CAP when the org's
    hourly slots are used up or the counter cannot be written; NOT_APPLIED for an org with no member resident. The org
    PROFILE and every member are read before the slot is taken, so a refusal for those never uses one."""
    if org_admin.admin_role is not AdminRole.SENDER or not profiles.is_linked_to_cognito(org_admin.user_id):
        logger.info("org_broadcast_send by %s refused: role %s or no passkey link", org_admin.user_id, org_admin.admin_role)
        channels.reject_action(connection_id, None, ActionOutcome.NOT_ALLOWED)
        return
    try:
        alert_text = org_broadcasts.parse_alert_text(body["text"])
    except (KeyError, ValueError):
        channels.reject_action(connection_id, None, ActionOutcome.BAD_MESSAGE)
        return
    unsafe_kinds = org_broadcasts.unsafe_kinds_in(alert_text)
    if unsafe_kinds:
        logger.info("org_broadcast_send by %s refused: %s", org_admin.user_id, ", ".join(unsafe_kinds))
        channels.reject_action(connection_id, None, ActionOutcome.TEXT_REFUSED)
        return
    org_profile = orgs.read_org_profile(org_admin.org_id)
    if org_profile is None:
        raise LookupError(f"org {org_admin.org_id} has no PROFILE: its alert has no sender name")
    recipients = org_broadcasts.list_alert_recipients(org_admin.org_id)
    if not recipients:
        logger.info("org_broadcast_send by %s not applied: org %s has no member resident", org_admin.user_id, org_admin.org_id)
        channels.reject_action(connection_id, None, ActionOutcome.NOT_APPLIED)
        return
    if not org_broadcasts.take_broadcast_slot(org_admin.org_id, int(os.environ[BROADCASTS_PER_ORG_PER_HOUR_ENVIRONMENT_NAME])):
        logger.info("org_broadcast_send by %s refused: over the hourly cap of org %s", org_admin.user_id, org_admin.org_id)
        channels.reject_action(connection_id, None, ActionOutcome.OVER_CAP)
        return
    sent_alert = org_broadcasts.write_safety_alert(org_admin, org_profile.org_name, alert_text, recipients)
    logger.info("broadcast %s sent by %s in org %s to %d members", sent_alert.broadcast_id, org_admin.user_id, org_admin.org_id,
                len(sent_alert.recipient_rows))
    push_sent_alert(org_admin.org_id, sent_alert)


def push_sent_alert(org_id: str, sent_alert: org_broadcasts.SentAlert) -> None:
    """Each member's open sockets get her notice and the org's admins org_changed. The rows are already stored, so a member
    whose app is closed sees the alert on her next sync; this role may not delete CONN# rows, so gone sockets are left."""
    for recipient_row in sent_alert.recipient_rows:
        notice_message = {"type": PushMessageType.ORG_NOTICE, "notice": org_broadcasts.notice_for_resident(recipient_row)}
        channels.push_to_user(recipient_row["residentUserId"], notice_message, should_remove_gone_connections=False)
    channels.push_to_user(org_user_id_for(org_id), {"type": PushMessageType.ORG_CHANGED}, should_remove_gone_connections=False)
