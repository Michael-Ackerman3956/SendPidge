"""pidge-verdict's org admin paths: the admin's sync (the office's "Was this you?" cards and the home's safety overview) and
the audit of an admin's answer as the office. Each acts for the OrgAdmin resolved from the caller's own PROFILE and
ORG#<orgId>/ADMIN# rows (orgs.require_org_admin), and writes its ORGAUDIT# row before anything is pushed or recorded: a failed
audit write raises, so nothing is sent (fail closed)."""

import hmac
from datetime import datetime, timezone

from shared import graph, org_audit, org_view, orgs, profiles
from shared.enums import PushMessageType, UserRole
from shared.org_enums import OrgAuditAction
from shared.org_ids import ORG_CONTACT_SORT_KEY_PREFIX, is_org_contact_sort_key, org_user_id_for
from shared.orgs import OrgAdmin


def org_admin_sync_message(org_admin: OrgAdmin) -> dict | None:
    """The admin's sync_state: the cards asked of org-<orgId> (never of the admin's own id) and the org dashboard, after
    the member_view audit row (targetRef the org id) is written. None, auditing nothing, when the org has no PROFILE row
    (org_view logs it)."""
    from pidge_verdict.handler import contact_cards_for  # at call time: handler imports this module

    dashboard = org_view.org_dashboard_for(org_admin, datetime.now(timezone.utc))
    if dashboard is None:
        return None
    message = {"type": PushMessageType.SYNC_STATE, "role": UserRole.ORG_ADMIN,
               "requests": contact_cards_for(org_user_id_for(org_admin.org_id)), "org": dashboard}
    org_audit.write_audit_row(org_admin, OrgAuditAction.MEMBER_VIEW, org_admin.org_id)
    return message


def audit_office_answer(user_id: str, request_row: dict) -> OrgAdmin | None:
    """For an in-app answer on a request held for a home's office (contactSK CONTACT#org-<orgId>) by a complete admin of
    that same org: writes the ORGAUDIT# answer row (targetRef the request id) and returns the admin; a failed write raises,
    so the caller records no answer. Any other request or caller: None and nothing written; record_answer's
    is_expected_sender then refuses a non-admin, counted once there (the role is read here without counting a refusal)."""
    contact_sort_key = request_row["contactSK"]
    if not is_org_contact_sort_key(contact_sort_key):
        return None
    profile_row = profiles.read_profile_row(f"USER#{user_id}")
    if profile_row is None or "role" not in profile_row or profile_row["role"] != UserRole.ORG_ADMIN:
        return None
    org_admin = orgs.require_org_admin(user_id)
    request_org_id = contact_sort_key.removeprefix(ORG_CONTACT_SORT_KEY_PREFIX)
    if org_admin is None or not hmac.compare_digest(org_admin.org_id.encode(), request_org_id.encode()):
        return None
    org_audit.write_audit_row(org_admin, OrgAuditAction.ANSWER, graph.request_id_of(request_row))
    return org_admin
