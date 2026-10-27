"""The ids an org is known by: org id <orgId>, its user id "org-<orgId>" (the office's contact identity in a resident's circle)
and the resident's CONTACT#org-<orgId> sort key. Imports only re, so any shared module can use it without a cycle."""

import re

ORG_USER_ID_PREFIX = "org-"
ORG_CONTACT_SORT_KEY_PREFIX = "CONTACT#org-"
ORG_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")  # used with fullmatch


def org_user_id_for(org_id: str) -> str:
    """The org's user id "org-<orgId>"; raises ValueError when the org id is not text matching ORG_ID_PATTERN."""
    if not isinstance(org_id, str) or not ORG_ID_PATTERN.fullmatch(org_id):
        raise ValueError("org id")
    return f"{ORG_USER_ID_PREFIX}{org_id}"


def is_org_user_id(user_id: str) -> bool:
    return user_id.startswith(ORG_USER_ID_PREFIX)


def org_id_of_org_user(user_id: str) -> str:
    """The org id inside "org-<orgId>"; raises ValueError when the user id has no "org-" prefix or the rest is off-pattern."""
    if not is_org_user_id(user_id):
        raise ValueError("not an org user id")
    org_id = user_id.removeprefix(ORG_USER_ID_PREFIX)
    if not ORG_ID_PATTERN.fullmatch(org_id):
        raise ValueError("org id")
    return org_id


def is_org_contact_sort_key(sort_key: str) -> bool:
    return sort_key.startswith(ORG_CONTACT_SORT_KEY_PREFIX)


def org_contact_sort_key_for(org_id: str) -> str:
    return f"CONTACT#{org_user_id_for(org_id)}"
