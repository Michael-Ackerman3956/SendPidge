"""What an org admin sees of their own org: its name and its members, each named from the resident's own
RECEIVER#<address>/PROFILE row. Built only for the OrgAdmin orgs.require_org_admin resolved, never for a body org id."""

import logging

from shared import orgs, profiles
from shared.metrics import emit_count_metric
from shared.orgs import OrgAdmin
from shared.profiles import UnknownReceiverError

logger = logging.getLogger(__name__)
ORG_MEMBER_REJECTED_METRIC_NAME = "OrgMemberRejected"


def org_dashboard_for(org_admin: OrgAdmin) -> dict | None:
    """{"orgId", "orgName", "members": [{"memberRef", "residentName", "tags"}]}; None (logged) when the org has no PROFILE
    row. A member without a resident profile, or whose resident belongs to another org, is left out and logged."""
    org_profile = orgs.read_org_profile(org_admin.org_id)
    if org_profile is None:
        logger.warning("org %s has no PROFILE row; admin %s gets no dashboard", org_admin.org_id, org_admin.user_id)
        return None
    members = []
    for member_row in orgs.list_member_rows(org_admin.org_id):
        member = member_for_dashboard(org_admin.org_id, member_row)
        if member is not None:
            members.append(member)
    return {"orgId": org_profile.org_id, "orgName": org_profile.org_name, "members": members}


def member_for_dashboard(org_id: str, member_row: dict) -> dict | None:
    """One member's row on the dashboard, or None (logged; never the address) when the resident has no profile (counted
    UnknownReceiver by require_receiver_profile) or names another org (counted OrgMemberRejected). Tags are absent on rows the
    seed did not write."""
    member_ref = member_row["SK"].removeprefix(orgs.MEMBER_SORT_KEY_PREFIX)
    try:
        resident = profiles.require_receiver_profile(member_ref)
    except UnknownReceiverError:
        logger.warning("a member of org %s has no resident profile; left off the dashboard", org_id)
        return None
    if resident.org_id != org_id:
        logger.warning("member %s is listed in org %s but enrolled in another org; left off the dashboard",
                       resident.receiver_user_id, org_id)
        emit_count_metric(ORG_MEMBER_REJECTED_METRIC_NAME)
        return None
    tags = list(member_row["tags"]) if "tags" in member_row else []
    return {"memberRef": member_ref, "residentName": resident.resident_name, "tags": tags}
