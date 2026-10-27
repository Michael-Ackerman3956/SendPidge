"""Orgs written by the production seed and contact-row code, for tests that need ORG# rows and a home office in a circle."""

from shared import graph, orgs
from shared.org_enums import AdminRole
from shared.orgs import AdminSeed, MemberSeed, OrgSeed
from tests.helpers.profiles import TEST_TIMEZONE


def seed_test_org(org_id: str, admin_user_ids: list[str], member_addresses: list[str], name_variants: tuple[str, ...] = (),
                  admin_role: AdminRole = AdminRole.VIEWER) -> OrgSeed:
    """Runs orgs.validate_org_seed then orgs.write_org_seed and returns the validated seed: the org "<Org id> Commons"
    (e.g. "Sunrise Commons"), verified from office@<orgId>.example, every admin with admin_role, every member untagged.
    Each member must already be a resident of this org: seed_resident(..., org_id=org_id)."""
    org_seed = OrgSeed(org_id, f"{org_id.capitalize()} Commons", (f"office@{org_id}.example",), name_variants, TEST_TIMEZONE,
                       tuple(AdminSeed(user_id, admin_role) for user_id in admin_user_ids),
                       tuple(MemberSeed(address, ()) for address in member_addresses))
    validated_seed = orgs.validate_org_seed(org_seed)
    orgs.write_org_seed(validated_seed)
    return validated_seed


def seed_org_contact(receiver_address: str, org_id: str) -> dict:
    """Puts the org's CONTACT#org-<orgId> row, built by orgs.org_contact_row from its stored PROFILE (seed_test_org first),
    in the resident's circle and returns it."""
    org_profile = orgs.read_org_profile(org_id)
    assert org_profile is not None, "seed the org first"
    contact_row = orgs.org_contact_row(receiver_address, org_profile, graph.now_timestamp())
    graph.get_table().put_item(Item=contact_row)
    return contact_row
