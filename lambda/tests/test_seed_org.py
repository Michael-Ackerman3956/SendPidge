"""The org seed: one transaction of ORG# rows and admin roles that never writes a RECEIVER# row; a re-seed rewrites the tags,
keeps what other writers SET on MEMBER# and deletes the ADMIN# row of every admin it no longer lists; a resident's user id is
refused as an admin with nothing written."""

from dataclasses import replace

import pytest

from shared import graph, orgs
from shared.org_enums import AdminRole
from shared.orgs import AdminSeed, MemberSeed, OrgAdmin, OrgProfile, OrgSeed
from tests.helpers.profiles import TEST_TIMEZONE, seed_resident
from tests.test_verdict import aws  # noqa: F401

GRANDMA_ADDRESS = "grandma@example.com"
SUNRISE_SEED = OrgSeed("sunrise", " Sunrise Commons ", ("Office@Sunrise.example",), ("Sunrise Commons Billing",), TEST_TIMEZONE,
                       (AdminSeed("ann", AdminRole.VIEWER),), (MemberSeed(" Grandma@Example.com", ("memory care",)),))


def read_all_rows() -> list[dict]:
    return sorted(graph.get_table().scan()["Items"], key=lambda row: (row["PK"], row["SK"]))


def read_receiver_partition_rows() -> list[dict]:
    return [row for row in read_all_rows() if row["PK"].startswith("RECEIVER#")]


def test_seed_writes_no_receiver_partition_and_refuses_a_resident_as_admin(aws):  # noqa: F811
    seed_resident(GRANDMA_ADDRESS, "grandma", "Grandma", org_id="sunrise")
    receiver_rows_before = read_receiver_partition_rows()

    orgs.write_org_seed(orgs.validate_org_seed(SUNRISE_SEED))

    assert read_receiver_partition_rows() == receiver_rows_before
    assert orgs.read_org_profile("sunrise") == OrgProfile("sunrise", "Sunrise Commons", ("office@sunrise.example",),
                                                          ("Sunrise Commons Billing",), TEST_TIMEZONE)
    assert orgs.require_org_admin("ann") == OrgAdmin("ann", "sunrise", AdminRole.VIEWER)
    assert not [row for row in read_all_rows() if row["PK"].startswith("TOKEN#") or row["PK"] == "USER#org-sunrise"]
    [member_row] = orgs.list_member_rows("sunrise")
    assert (member_row["SK"], member_row["tags"]) == (f"MEMBER#{GRANDMA_ADDRESS}", ["memory care"])

    # A field the S2b visibility writer SETs on MEMBER# survives a re-seed that changes the tags.
    graph.get_table().update_item(Key={"PK": "ORG#sunrise", "SK": f"MEMBER#{GRANDMA_ADDRESS}"},
                                  UpdateExpression="SET coverageVisibility = :visibility",
                                  ExpressionAttributeValues={":visibility": "alerts_only"})
    orgs.write_org_seed(orgs.validate_org_seed(replace(SUNRISE_SEED, members=(MemberSeed(GRANDMA_ADDRESS, ("hearing",)),))))
    [member_row] = orgs.list_member_rows("sunrise")
    assert (member_row["tags"], member_row["coverageVisibility"]) == (["hearing"], "alerts_only")

    rows_before_refusal = read_all_rows()
    resident_as_admin_seed = replace(SUNRISE_SEED, org_name="Renamed Commons",
                                     admins=(AdminSeed("ann", AdminRole.SENDER), AdminSeed("grandma", AdminRole.VIEWER)))
    with pytest.raises(ValueError):
        orgs.write_org_seed(orgs.validate_org_seed(resident_as_admin_seed))
    assert read_all_rows() == rows_before_refusal


def test_reseed_without_an_admin_deletes_their_admin_row(aws):  # noqa: F811
    seed_resident(GRANDMA_ADDRESS, "grandma", "Grandma", org_id="sunrise")
    ann_and_amy_seed = replace(SUNRISE_SEED, admins=(AdminSeed("ann", AdminRole.VIEWER), AdminSeed("amy", AdminRole.SENDER)))
    orgs.write_org_seed(orgs.validate_org_seed(ann_and_amy_seed))
    assert orgs.require_org_admin("amy") == OrgAdmin("amy", "sunrise", AdminRole.SENDER)

    orgs.write_org_seed(orgs.validate_org_seed(SUNRISE_SEED))  # amy is no longer listed
    assert orgs.list_admin_user_ids("sunrise") == ["ann"]
    assert orgs.require_org_admin("amy") is None
    assert orgs.require_org_admin("ann") == OrgAdmin("ann", "sunrise", AdminRole.VIEWER)

    rows_before_refusal = read_all_rows()  # dropping ann in a seed that fails deletes nothing: one transaction
    with pytest.raises(ValueError):
        orgs.write_org_seed(orgs.validate_org_seed(replace(SUNRISE_SEED, admins=(AdminSeed("grandma", AdminRole.VIEWER),))))
    assert read_all_rows() == rows_before_refusal
