"""Feed one .eml to the Reader and print the verdict and the policy decision, offline except for the model call.

Usage: python scripts/read_eml.py samples/fake-jimin.eml [--contact Jimin ...] [--known-address jimin@example.com ...]
                                  [--escalation hold|warn]
--known-address also supplies the family domains that link lookalikes are judged against.
--known-address lists the addresses the contacts are known to write from (gate 1 holds a contact's name from any other).
--escalation fakes the second reviewer's answer (its claimed_person echoes the Reader's) so the escalation path runs offline.
Needs AWS_PROFILE (or ambient credentials) and READER_MODEL in the environment; see .env.example.
"""

import argparse
import json
import os
import sys
from pathlib import Path

import boto3
from strands.types.exceptions import StructuredOutputException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambda"))

from shared.auth import authentication_reasons  # noqa: E402
from shared.email_parse import parse_eml  # noqa: E402
from shared.enums import EscalationDecision, ReaderModelChoice  # noqa: E402
from shared.graph import Contact, domains_of, find_contact_among  # noqa: E402
from shared.links import assess_links, link_reasons  # noqa: E402
from shared.lists import SeedListStore, list_row_from_seed, lookup_sender_reasons  # noqa: E402
from shared.policy import EscalationAnswer, ReaderFailure, apply_escalation, decide, needs_escalation  # noqa: E402
from shared.reader_agent import BEDROCK_REGION, build_reader, read_email  # noqa: E402
from shared.sanitize import sanitize_summary  # noqa: E402

DEFAULT_CONTACT_NAMES = ["Jimin"]


def read_required_setting(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is not set; copy .env.example to .env and export it")
    return value


class ConverseRequestRecorder:
    """Records how Strands asked Bedrock for structured output: which tools, and whether outputConfig carried the schema."""

    def __init__(self) -> None:
        self.tool_names: list[str] = []
        self.output_schema_name: str | None = None

    def attach(self, session: boto3.Session) -> None:
        session.events.register("provide-client-params.bedrock-runtime.*", self.record)

    def record(self, params: dict, **_: object) -> None:
        """params are the Converse / ConverseStream keyword arguments, before serialisation."""
        self.tool_names = [tool["toolSpec"]["name"] for tool in params.get("toolConfig", {}).get("tools", []) if "toolSpec" in tool]
        structure = params.get("outputConfig", {}).get("textFormat", {}).get("structure", {})
        self.output_schema_name = structure.get("jsonSchema", {}).get("name")

    def describe(self) -> str:
        return f"tools sent: {self.tool_names or 'none'} · outputConfig schema: {self.output_schema_name or 'none'}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("eml_path", type=Path)
    parser.add_argument("--contact", action="append", dest="contact_names", default=None, help="a known contact name; repeatable")
    parser.add_argument("--known-address", action="append", dest="known_addresses", default=[],
                        help="an address every named contact is known to write from; repeatable")
    parser.add_argument("--escalation", type=EscalationDecision, choices=list(EscalationDecision), default=None,
                        help="fake the second reviewer's decision instead of calling Opus")
    parser.add_argument("--lists", type=Path, default=None,
                        help="a scam-list seed file (infra/seed/lists.sample.json) read in memory instead of DynamoDB")
    arguments = parser.parse_args()
    contact_names = arguments.contact_names or DEFAULT_CONTACT_NAMES
    known_addresses = tuple(address.casefold() for address in arguments.known_addresses)
    contacts = [Contact(f"CONTACT#{name.casefold()}", name, name.casefold(), known_addresses=known_addresses) for name in contact_names]

    model_choice = ReaderModelChoice(read_required_setting("READER_MODEL"))
    session = boto3.Session(profile_name=os.environ.get("AWS_PROFILE"), region_name=BEDROCK_REGION)
    recorder = ConverseRequestRecorder()
    recorder.attach(session)

    parsed_email = parse_eml(arguments.eml_path.read_bytes())
    family_domains = domains_of(contacts)
    link_assessments = assess_links(parsed_email.links, family_domains)
    reasons = authentication_reasons(parsed_email.pidge_auth_header_values)
    reasons.extend(link_reasons(link_assessments, parsed_email))
    if arguments.lists is not None:
        list_store = SeedListStore([list_row_from_seed(seed_row) for seed_row in json.loads(arguments.lists.read_text())])
        reasons.extend(lookup_sender_reasons(list_store, parsed_email.sender_address, parsed_email.sender_domain, parsed_email.sender_name, contacts))
    for assessment in link_assessments:
        print(f"link: text={assessment.link.visible_text!r} domain={assessment.link.registrable_domain} flagged={assessment.is_flagged}")
    reader = build_reader(model_choice, session)
    try:
        verdict = read_email(reader, parsed_email)
        reader_outcome = verdict
        print(f"claimed_person={verdict.claimed_person}  intent={verdict.intent}  confidence={verdict.confidence:.2f}")
        print(f"summary: {sanitize_summary(verdict.summary)}")
    except StructuredOutputException as error:
        reader_outcome = ReaderFailure(str(error))
        print(f"reader failed: {error}")

    contact_by_name = find_contact_among(contacts, parsed_email.sender_name)
    contact_by_claim = find_contact_among(contacts, getattr(reader_outcome, "claimed_person", None))
    outcome = decide(reader_outcome, contact_by_name, contact_by_claim, parsed_email.sender_address, reasons)
    if needs_escalation(outcome, arguments.escalation is not None):
        print(f"escalation (faked): decision={arguments.escalation} applied to {outcome.gate}")
        outcome = apply_escalation(outcome, EscalationAnswer(arguments.escalation, contact_by_claim))
    print(f"decision={outcome.decision.upper()}  warn_reason={outcome.warn_reason}  gate={outcome.gate}  "
          f"friction={outcome.friction}  reasons={[reason.value for reason in outcome.reasons]}")
    print(f"converse request: {recorder.describe()}")


if __name__ == "__main__":
    main()
