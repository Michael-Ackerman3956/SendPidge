"""Resident profiles written by the production enrolment code, for tests that need RECEIVER#/PROFILE and role rows."""

from shared import residents
from shared.residents import ResidentEnrolment

TEST_ORG_ID = "org-test"
TEST_TIMEZONE = "America/New_York"


def seed_resident(receiver_address: str, user_id: str, resident_name: str) -> None:
    residents.write_resident_rows(ResidentEnrolment(receiver_address, user_id, resident_name, TEST_ORG_ID, TEST_TIMEZONE))


def seed_contact_roles(receiver_address: str) -> list[str]:
    return residents.mark_circle_contacts(receiver_address)
