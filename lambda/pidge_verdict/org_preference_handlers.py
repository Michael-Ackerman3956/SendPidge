"""pidge-verdict's resident org actions: set_org_visibility (what the home's office may see) and add_org_contact /
remove_org_contact (the office's row in the circle). Each acts for the ReceiverProfile route_resident_action resolved from
the caller's own PROFILE row; the org is that resident's own orgId, never the body. A refusal goes back to the caller's
socket; after a RECORDED action every receiver socket gets the circle and the home section (push_circle_state)."""

import logging
from datetime import datetime, timezone

from pidge_verdict import circle_handlers
from shared import channels, org_preferences
from shared.enums import ActionOutcome, ClientAction
from shared.org_enums import OrgVisibility
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
