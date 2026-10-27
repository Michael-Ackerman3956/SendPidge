"""pidge-verdict's org admin sync: the office's "Was this you?" cards and the home's member list. Each call acts for the
OrgAdmin the handler resolved from the caller's own PROFILE and ORG#<orgId>/ADMIN# rows (orgs.require_org_admin)."""

from shared import org_view
from shared.enums import PushMessageType, UserRole
from shared.org_ids import org_user_id_for
from shared.orgs import OrgAdmin


def org_admin_sync_message(org_admin: OrgAdmin) -> dict | None:
    """The admin's sync_state: the cards asked of org-<orgId> (never of the admin's own id) and the org dashboard. None when
    the org has no PROFILE row (org_view logs it)."""
    from pidge_verdict.handler import contact_cards_for  # at call time: handler imports this module

    dashboard = org_view.org_dashboard_for(org_admin)
    if dashboard is None:
        return None
    return {"type": PushMessageType.SYNC_STATE, "role": UserRole.ORG_ADMIN,
            "requests": contact_cards_for(org_user_id_for(org_admin.org_id)), "org": dashboard}
