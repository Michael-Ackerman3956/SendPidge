"""Closed sets used only by org code (a home's office and its admins). Code branches on these, never on bare strings."""

from enum import StrEnum


class AdminRole(StrEnum):
    """What an org admin may do, stored on ORG#<orgId>/ADMIN#<userId>.role and written only by the org seed."""

    VIEWER = "viewer"
    SENDER = "sender"
