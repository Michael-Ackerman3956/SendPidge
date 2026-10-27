"""The resident's choices about their home (org): what its office may see, RECEIVER#<address>/ORGPREF#<orgId>
{orgVisibility, orgVisibilitySetAt, orgVisibilitySource}, and whether the office's CONTACT#org-<orgId> row is in the circle.
The org is always the resident's own orgId from RECEIVER#/PROFILE, never a request body: each writer acts for the
ReceiverProfile pidge_verdict's route_resident_action resolved from the caller's own PROFILE row."""

from datetime import datetime

from botocore.exceptions import ClientError

from shared import circle, graph, orgs
from shared.enums import ActionOutcome
from shared.org_enums import OrgPreferenceSource, OrgVisibility
from shared.org_ids import org_contact_sort_key_for
from shared.profiles import ReceiverProfile

ORG_PREFERENCE_SORT_KEY_PREFIX = "ORGPREF#"


def org_preference_key(receiver_address: str, org_id: str) -> dict:
    return {"PK": f"RECEIVER#{receiver_address}", "SK": f"{ORG_PREFERENCE_SORT_KEY_PREFIX}{org_id}"}


def read_org_visibility(receiver_address: str, org_id: str) -> OrgVisibility:
    """The resident's stored choice. No ORGPREF# row, or a row without orgVisibility, is NONE: nothing chosen means the
    office sees nothing. A value outside OrgVisibility raises ValueError."""
    response = graph.get_table().get_item(Key=org_preference_key(receiver_address, org_id))
    if "Item" not in response or "orgVisibility" not in response["Item"]:
        return OrgVisibility.NONE
    return OrgVisibility(response["Item"]["orgVisibility"])


def set_org_visibility(resident: ReceiverProfile, visibility: OrgVisibility, now: datetime) -> ActionOutcome:
    """SETs the choice, its time and its source on the resident's ORGPREF#<orgId> row. NOT_ALLOWED when the resident's org
    has no ORG#<orgId>/PROFILE row."""
    if orgs.read_org_profile(resident.org_id) is None:
        return ActionOutcome.NOT_ALLOWED
    graph.get_table().update_item(
        Key=org_preference_key(resident.receiver_address, resident.org_id),
        UpdateExpression="SET orgVisibility = :visibility, orgVisibilitySetAt = :setAt, orgVisibilitySource = :source",
        ExpressionAttributeValues={":visibility": str(visibility), ":setAt": graph.iso_timestamp(now),
                                   ":source": str(OrgPreferenceSource.RESIDENT_APP)},
    )
    return ActionOutcome.RECORDED


def is_org_name_taken(contact_rows: list[dict], org_profile: orgs.OrgProfile) -> bool:
    """Whether a contact in the circle, the office's own row aside, is named the org's name or one of its name variants
    (compared as circle.find_same_name_row compares names: case and spacing ignored)."""
    office_sort_key = org_contact_sort_key_for(org_profile.org_id)
    other_rows = [row for row in contact_rows if row["SK"] != office_sort_key]
    return any(circle.find_same_name_row(other_rows, graph.normalize_person_name(name)) is not None
               for name in (org_profile.org_name, *org_profile.name_variants))


def add_org_contact(resident: ReceiverProfile, now: datetime) -> ActionOutcome:
    """Puts the office's row, built only by orgs.org_contact_row, in the resident's circle. NOT_ALLOWED when the org has no
    PROFILE; DUPLICATE_NAME when a contact already has the org's name or a name variant (asks are matched by name);
    NOT_APPLIED when the office's row is already there."""
    org_profile = orgs.read_org_profile(resident.org_id)
    if org_profile is None:
        return ActionOutcome.NOT_ALLOWED
    if is_org_name_taken(graph.list_contact_rows(resident.receiver_address), org_profile):
        return ActionOutcome.DUPLICATE_NAME
    contact_row = orgs.org_contact_row(resident.receiver_address, org_profile, graph.iso_timestamp(now))
    try:
        graph.get_table().put_item(Item=contact_row, ConditionExpression="attribute_not_exists(PK)")
    except ClientError as error:
        if error.response["Error"]["Code"] == graph.CONDITIONAL_CHECK_FAILED:
            return ActionOutcome.NOT_APPLIED
        raise
    return ActionOutcome.RECORDED


def remove_org_contact(resident: ReceiverProfile) -> ActionOutcome:
    """Deletes the office's CONTACT#org-<orgId> row from the resident's circle; NOT_APPLIED when it is not there."""
    try:
        graph.get_table().delete_item(
            Key={"PK": f"RECEIVER#{resident.receiver_address}", "SK": org_contact_sort_key_for(resident.org_id)},
            ConditionExpression="attribute_exists(PK)",
        )
    except ClientError as error:
        if error.response["Error"]["Code"] == graph.CONDITIONAL_CHECK_FAILED:
            return ActionOutcome.NOT_APPLIED
        raise
    return ActionOutcome.RECORDED


def org_preference_for_sync(resident: ReceiverProfile) -> dict | None:
    """The home section of the resident's PWA: the org's name, the stored visibility and whether the office's row is in the
    circle. None when the resident's org has no PROFILE row (the section is hidden)."""
    org_profile = orgs.read_org_profile(resident.org_id)
    if org_profile is None:
        return None
    office_row = graph.get_contact_row(resident.receiver_address, org_contact_sort_key_for(org_profile.org_id))
    return {"orgName": org_profile.org_name, "visibility": str(read_org_visibility(resident.receiver_address, org_profile.org_id)),
            "isOrgContact": office_row is not None}
