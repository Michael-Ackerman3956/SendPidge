"""Closed sets used only by org code (a home's office and its admins). Code branches on these, never on bare strings."""

from enum import StrEnum


class AdminRole(StrEnum):
    """What an org admin may do, stored on ORG#<orgId>/ADMIN#<userId>.role and written only by the org seed."""

    VIEWER = "viewer"
    SENDER = "sender"


class OrgVisibility(StrEnum):
    """What the resident lets the home's office see, stored on RECEIVER#<address>/ORGPREF#<orgId>.orgVisibility by the
    resident's own set_org_visibility. No row or no field reads as NONE (shared/org_preferences.read_org_visibility)."""

    ALERTS_ONLY = "alerts_only"
    ALERTS_AND_CASES = "alerts_and_cases"
    NONE = "none"


class OrgPreferenceSource(StrEnum):
    """Who wrote RECEIVER#<address>/ORGPREF#<orgId>, stored as orgVisibilitySource."""

    RESIDENT_APP = "resident_app"  # the resident's own PWA
