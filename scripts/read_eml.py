"""Feed one .eml to the Reader and print the verdict and the policy decision.

Usage: python scripts/read_eml.py samples/fake-jimin.eml [--contact Jimin ...]
Needs AWS_PROFILE (or ambient credentials) and READER_MODEL in the environment; see .env.example.
"""

import argparse
import os
import sys
from pathlib import Path

import boto3
from strands.types.exceptions import StructuredOutputException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambda"))

from shared.auth import authentication_reasons  # noqa: E402
from shared.email_parse import parse_eml  # noqa: E402
from shared.enums import ReaderModelChoice  # noqa: E402
from shared.policy import ReaderFailure, decide  # noqa: E402
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


def is_claimed_person_a_contact(claimed_person: str | None, contact_names: list[str]) -> bool:
    if claimed_person is None:
        return False
    return claimed_person.strip().casefold() in {name.casefold() for name in contact_names}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("eml_path", type=Path)
    parser.add_argument("--contact", action="append", dest="contact_names", default=None, help="a known contact name; repeatable")
    arguments = parser.parse_args()
    contact_names = arguments.contact_names or DEFAULT_CONTACT_NAMES

    model_choice = ReaderModelChoice(read_required_setting("READER_MODEL"))
    session = boto3.Session(profile_name=os.environ.get("AWS_PROFILE"), region_name=BEDROCK_REGION)
    recorder = ConverseRequestRecorder()
    recorder.attach(session)

    parsed_email = parse_eml(arguments.eml_path.read_bytes())
    reasons = authentication_reasons(parsed_email.pidge_auth_header_values)
    reader = build_reader(model_choice, session)
    try:
        verdict = read_email(reader, parsed_email)
        reader_outcome = verdict
        print(f"claimed_person={verdict.claimed_person}  intent={verdict.intent}  confidence={verdict.confidence:.2f}")
        print(f"summary: {sanitize_summary(verdict.summary)}")
    except StructuredOutputException as error:
        reader_outcome = ReaderFailure(str(error))
        print(f"reader failed: {error}")

    outcome = decide(reader_outcome, is_claimed_person_a_contact(getattr(reader_outcome, "claimed_person", None), contact_names), reasons)
    print(f"decision={outcome.decision.upper()}  warn_reason={outcome.warn_reason}  reasons={[reason.value for reason in outcome.reasons]}")
    print(f"converse request: {recorder.describe()}")


if __name__ == "__main__":
    main()
