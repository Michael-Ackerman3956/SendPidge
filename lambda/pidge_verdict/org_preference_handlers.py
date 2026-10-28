"""pidge-verdict's resident org actions: set_org_visibility (what the home's office may see), add_org_contact /
remove_org_contact (the office's row in the circle) and org_broadcast_ack (Got it / Need help on her home's alert). Each acts
for the ReceiverProfile route_resident_action resolved from the caller's own PROFILE row; the org is that resident's own
orgId, never the body. A refusal goes back to the caller's socket; after a RECORDED circle action every receiver socket gets
the circle and the home section (push_circle_state)."""

import logging
from datetime import datetime, timezone

from pidge_verdict import circle_handlers
from shared import channels, org_broadcasts, org_preferences
from shared.enums import ActionOutcome, ClientAction, PushMessageType
from shared.org_enums import AckAnswer, OrgVisibility
from shared.org_ids import org_user_id_for
from shared.profiles import ReceiverProfile

logger = logging.getLogger(__name__)


def handle_set_org_visibility(connection_id: str, resident: ReceiverProfile, body: dict) -> None:
    """A visibility outside OrgVisibility is BAD_MESSAGE and writes nothing."""
    try:
        visibility = OrgVisibility(body["visibility"])
    except (KeyError, TypeError, ValueError):
        channels.reject_action(connection_id, None, ActionOutcome.BAD_MESSAGE)
        return
    outcome = org_preferences.set_org_visibility(resident, visibility, datetime.now(timezone.utc))
    answer_org_action(connection_id, resident, ClientAction.SET_ORG_VISIBILITY, outcome)


def handle_add_org_contact(connection_id: str, resident: ReceiverProfile, _body: dict) -> None:
    outcome = org_preferences.add_org_contact(resident, datetime.now(timezone.utc))
    answer_org_action(connection_id, resident, ClientAction.ADD_ORG_CONTACT, outcome)


def handle_remove_org_contact(connection_id: str, resident: ReceiverProfile, _body: dict) -> None:
    answer_org_action(connection_id, resident, ClientAction.REMOVE_ORG_CONTACT, org_preferences.remove_org_contact(resident))


def answer_org_action(connection_id: str, resident: ReceiverProfile, action: ClientAction, outcome: ActionOutcome) -> None:
    if outcome is not ActionOutcome.RECORDED:
        logger.info("%s refused for %s: %s", action, resident.receiver_user_id, outcome)
        channels.reject_action(connection_id, None, outcome)
        return
    logger.info("%s recorded for %s", action, resident.receiver_user_id)
    circle_handlers.push_circle_state(circle_handlers.read_settings(), resident)


def handle_org_broadcast_ack(connection_id: str, resident: ReceiverProfile, body: dict) -> None:
    """BAD_MESSAGE for a broadcastId off its pattern or an answer outside AckAnswer; NOT_ALLOWED when she is not a recipient
    of that alert of her own home; NOT_APPLIED when she already answered it. RECORDED: her sockets get the answered notice
    and her home's admins org_changed, so their report shows Got it or Need help."""
    try:
        broadcast_id = org_broadcasts.parse_broadcast_id(body["broadcastId"])
        answer = AckAnswer(body["answer"])
    except (KeyError, TypeError, ValueError):
        channels.reject_action(connection_id, None, ActionOutcome.BAD_MESSAGE)
        return
    outcome, recipient_row = org_broadcasts.record_acknowledgement(resident, broadcast_id, answer)
    logger.info("%s of %s by %s: %s", ClientAction.ORG_BROADCAST_ACK, broadcast_id, resident.receiver_user_id, outcome)
    if outcome is not ActionOutcome.RECORDED:
        channels.reject_action(connection_id, None, outcome)
        return
    channels.push_to_user(resident.receiver_user_id,
                          {"type": PushMessageType.ORG_NOTICE, "notice": org_broadcasts.notice_for_resident(recipient_row)})
    channels.push_to_user(org_user_id_for(resident.org_id), {"type": PushMessageType.ORG_CHANGED})
