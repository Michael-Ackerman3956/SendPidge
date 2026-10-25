"""Read-only demo preflight: model access, the WhatsApp template, SNS subscriptions, RCS, MX, SES identities, the seeded
contact row and the deployed flags. Writes nothing anywhere; never prints secrets, full phone numbers or full mailboxes.

Prints one line per check (OK / FAIL / MOCK / SKIP) and exits 1 when any check FAILs.
Usage: AWS_PROFILE=sendpidge python scripts/preflight.py
"""

import functools
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT / "lambda"))

from shared.enums import ReaderModelChoice, VerifiedVia  # noqa: E402
from shared.reader_agent import BEDROCK_MODEL_IDS  # noqa: E402
from shared.whatsapp import TEMPLATE_LANGUAGE_CODE  # noqa: E402

STACK_NAME = "SendPidge"
REGION_NAME = "us-east-1"
CDK_CONTEXT = json.loads((REPOSITORY_ROOT / "infra" / "cdk.json").read_text())["context"]
PERSONAL_ENV_FILE = REPOSITORY_ROOT / ".env"  # the git-ignored file infra/bin/sendpidge.ts reads the mailboxes from
CONTACT_SORT_KEY = "CONTACT#jimin"
INFERENCE_PROFILE_PREFIXES = ("us.", "eu.", "apac.", "global.")
GRANTED_VALUES = {"authorizationStatus": "AUTHORIZED", "entitlementAvailability": "AVAILABLE"}
PENDING_SUBSCRIPTION_ARN = "PendingConfirmation"
RCS_BLOCKED_NOTE = "RCS to a Korean SIM is blocked (devpost/checklist.md slice 5, 2026-10-24); the demo uses in-app + WhatsApp"
CHECK_ERRORS = (ClientError, BotoCoreError, LookupError, ValueError, OSError, subprocess.SubprocessError)


class CheckStatus(StrEnum):
    OK = "OK"
    FAIL = "FAIL"
    MOCK = "MOCK"  # works, but on a stand-in (mock code, blocked channel)
    SKIP = "SKIP"  # not checked, with the reason


@dataclass(frozen=True)
class CheckResult:
    status: CheckStatus
    name: str
    detail: str


class DeployedStack:
    """The SendPidge stack's outputs and Lambda environments, read on first use (a failed read is retried, never cached)."""

    def __init__(self, session: boto3.Session) -> None:
        self.session = session

    @functools.cached_property
    def outputs(self) -> dict[str, str]:
        [stack] = self.session.client("cloudformation").describe_stacks(StackName=STACK_NAME)["Stacks"]
        return {output["OutputKey"]: output["OutputValue"] for output in stack["Outputs"]}

    @functools.cached_property
    def function_names_by_logical_id(self) -> dict[str, str]:
        paginator = self.session.client("cloudformation").get_paginator("list_stack_resources")
        return {resource["LogicalResourceId"]: resource["PhysicalResourceId"]
                for page in paginator.paginate(StackName=STACK_NAME) for resource in page["StackResourceSummaries"]
                if resource["ResourceType"] == "AWS::Lambda::Function"}

    def function_environment(self, construct_id: str) -> dict[str, str]:
        """The deployed env of the one function whose CDK logical id starts with construct_id (e.g. "VerdictFunction")."""
        matches = [name for logical_id, name in self.function_names_by_logical_id.items() if logical_id.startswith(construct_id)]
        if len(matches) != 1:
            raise LookupError(f"{len(matches)} functions in {STACK_NAME} start with {construct_id}")
        return self.session.client("lambda").get_function_configuration(FunctionName=matches[0])["Environment"]["Variables"]


def run_check(name: str, check_function: Callable[[], list[CheckResult]]) -> list[CheckResult]:
    """Any AWS, lookup or subprocess error becomes a FAIL line naming it; nothing is skipped silently."""
    try:
        return check_function()
    except CHECK_ERRORS as error:
        return [CheckResult(CheckStatus.FAIL, name, f"{type(error).__name__}: {error}")]


def mask_phone_number(phone_number: str) -> str:
    return f"***{phone_number[-2:]}"


def mask_mailbox(mailbox: str) -> str:
    local_part, _, domain = mailbox.partition("@")
    return f"{local_part[:1]}***@{domain}"


def base_model_ids_for(bedrock, model_id: str) -> list[str]:
    """An inference-profile id resolves to its foundation model ids (deduplicated across regions); a plain id is itself."""
    if not model_id.startswith(INFERENCE_PROFILE_PREFIXES):
        return [model_id]
    profile = bedrock.get_inference_profile(inferenceProfileIdentifier=model_id)
    if profile["status"] != "ACTIVE":
        raise LookupError(f"inference profile {model_id} is {profile['status']}")
    return sorted({model["modelArn"].rpartition("/")[2] for model in profile["models"]})


def check_model_access(bedrock, setting_name: str, setting_value: str) -> list[CheckResult]:
    name = f"Bedrock {setting_name}={setting_value}"
    model_choice = ReaderModelChoice(setting_value)
    if model_choice not in BEDROCK_MODEL_IDS:
        return [CheckResult(CheckStatus.SKIP, name, "no Bedrock model id for this choice in shared/reader_agent.py")]
    details, is_granted = [], True
    for base_model_id in base_model_ids_for(bedrock, BEDROCK_MODEL_IDS[model_choice]):
        availability = bedrock.get_foundation_model_availability(modelId=base_model_id)
        agreement_status = availability["agreementAvailability"]["status"]
        is_granted = is_granted and agreement_status == "AVAILABLE" and all(
            availability[field] == expected for field, expected in GRANTED_VALUES.items())
        details.append(f"{base_model_id} authorization={availability['authorizationStatus']} "
                       f"entitlement={availability['entitlementAvailability']} agreement={agreement_status} "
                       f"region={availability['regionAvailability']}")
    return [CheckResult(CheckStatus.OK if is_granted else CheckStatus.FAIL, name, "; ".join(details))]


def check_bedrock_models(session: boto3.Session, stack: DeployedStack) -> list[CheckResult]:
    """The reader and escalation models the deployed pidge-reader actually uses; metadata calls only, no invoke."""
    reader_environment = stack.function_environment("ReaderFunction")
    bedrock = session.client("bedrock")
    results = []
    for setting_name in ("READER_MODEL", "ESCALATION_MODEL"):
        def check_this_setting(setting_name: str = setting_name) -> list[CheckResult]:
            return check_model_access(bedrock, setting_name, reader_environment[setting_name])
        results.extend(run_check(f"Bedrock {setting_name}", check_this_setting))
    return results


def check_whatsapp_template(session: boto3.Session) -> list[CheckResult]:
    template_name = CDK_CONTEXT["whatsappTemplateName"]
    name = f"WhatsApp template {template_name}"
    if CDK_CONTEXT["whatsappEnabled"] is not True:
        return [CheckResult(CheckStatus.SKIP, name, "whatsappEnabled is false in cdk.json")]
    social = session.client("socialmessaging")
    phone_number = social.get_linked_whatsapp_business_account_phone_number(id=CDK_CONTEXT["whatsappPhoneNumberId"])
    waba_id = phone_number["linkedWhatsAppBusinessAccountId"]
    templates, next_token = [], None
    while True:
        page = social.list_whatsapp_message_templates(id=waba_id, **({"nextToken": next_token} if next_token else {}))
        templates.extend(template for template in page["templates"] if template["templateName"] == template_name)
        next_token = page.get("nextToken")
        if not next_token:
            break
    statuses = {template["templateLanguage"]: template["templateStatus"] for template in templates}
    is_approved = any(language.casefold() == TEMPLATE_LANGUAGE_CODE.casefold() and status == "APPROVED"
                      for language, status in statuses.items())
    detail = ", ".join(f"{language}={status}" for language, status in statuses.items()) or "not found on the WABA"
    return [CheckResult(CheckStatus.OK if is_approved else CheckStatus.FAIL, name, f"{TEMPLATE_LANGUAGE_CODE} needed; {detail}")]


def check_sns_subscriptions(session: boto3.Session, stack: DeployedStack) -> list[CheckResult]:
    sns = session.client("sns")
    results = []
    for output_key in ("RcsInboundTopicArn", "WhatsAppInboundTopicArn"):
        paginator = sns.get_paginator("list_subscriptions_by_topic")
        subscriptions = [subscription for page in paginator.paginate(TopicArn=stack.outputs[output_key])
                         for subscription in page["Subscriptions"]]
        pending_count = sum(subscription["SubscriptionArn"] == PENDING_SUBSCRIPTION_ARN for subscription in subscriptions)
        is_confirmed = bool(subscriptions) and pending_count == 0
        protocols = ", ".join(subscription["Protocol"] for subscription in subscriptions) or "none"
        results.append(CheckResult(CheckStatus.OK if is_confirmed else CheckStatus.FAIL, f"SNS {output_key}",
                                   f"{len(subscriptions)} subscription(s) [{protocols}], {pending_count} pending"))
    return results


def check_rcs_tester() -> list[CheckResult]:
    return [CheckResult(CheckStatus.MOCK, "RCS tester", f"{RCS_BLOCKED_NOTE} (rcsEnabled={CDK_CONTEXT['rcsEnabled']})")]


def check_receiver_mx(stack: DeployedStack) -> list[CheckResult]:
    receiver_domain = CDK_CONTEXT["receiverAddress"].partition("@")[2]
    dig_output = subprocess.run(["dig", "+short", "MX", receiver_domain], capture_output=True, text=True, timeout=10, check=True).stdout
    mx_hosts = [line.split()[-1].rstrip(".").casefold() for line in dig_output.splitlines() if line.strip()]
    ingress_host = stack.outputs["MailManagerARecord"].rstrip(".").casefold()
    status = CheckStatus.OK if ingress_host in mx_hosts else CheckStatus.FAIL
    return [CheckResult(status, f"MX {receiver_domain}", f"MX hosts {mx_hosts or 'none'}; Mail Manager ingress {ingress_host}")]


def read_personal_mailboxes() -> dict[str, str]:
    """RECEIVER_MAILBOX and MOCK_BANK_MAILBOX from ../.env, as infra/bin/sendpidge.ts reads them; absent keys are left out."""
    if not PERSONAL_ENV_FILE.exists():
        return {}
    mailboxes = {}
    for line in PERSONAL_ENV_FILE.read_text().splitlines():
        key, separator, value = line.partition("=")
        mailbox = value.strip().strip("\"'")
        if separator and key.strip() in ("RECEIVER_MAILBOX", "MOCK_BANK_MAILBOX") and mailbox:
            mailboxes[key.strip()] = mailbox
    return mailboxes


def is_identity_verified(ses, identity: str) -> bool:
    try:
        return ses.get_email_identity(EmailIdentity=identity)["VerifiedForSendingStatus"] is True
    except ses.exceptions.NotFoundException:
        return False


def check_ses_identities(session: boto3.Session, stack: DeployedStack) -> list[CheckResult]:
    """The sending domain always; in the SES sandbox also every recipient (verified as an address or through its domain)."""
    ses = session.client("sesv2")
    sending_domain = CDK_CONTEXT["receiptSender"].partition("@")[2]
    results = [CheckResult(CheckStatus.OK if is_identity_verified(ses, sending_domain) else CheckStatus.FAIL,
                           f"SES sending domain {sending_domain}", "VerifiedForSendingStatus")]
    if ses.get_account()["ProductionAccessEnabled"]:
        return [*results, CheckResult(CheckStatus.OK, "SES recipients", "production access: recipients need no verification")]
    contact_row = read_contact_row(session, stack) or {}
    recipients = {**read_personal_mailboxes(), "contactEmail": contact_row.get("contactEmail", "")}
    for label in ("RECEIVER_MAILBOX", "MOCK_BANK_MAILBOX", "contactEmail"):
        mailbox = recipients.get(label, "")
        if not mailbox:
            results.append(CheckResult(CheckStatus.SKIP, f"SES recipient {label}", "not set (.env or the CONTACT# row)"))
            continue
        is_verified = is_identity_verified(ses, mailbox) or is_identity_verified(ses, mailbox.partition("@")[2])
        results.append(CheckResult(CheckStatus.OK if is_verified else CheckStatus.FAIL, f"SES recipient {label}",
                                   f"{mask_mailbox(mailbox)} (sandbox: must be verified)"))
    return results


def read_contact_row(session: boto3.Session, stack: DeployedStack) -> dict | None:
    table = session.resource("dynamodb").Table(stack.outputs["TableName"])
    return table.get_item(Key={"PK": f"RECEIVER#{CDK_CONTEXT['receiverAddress']}", "SK": CONTACT_SORT_KEY}).get("Item")


def check_contact_row(session: boto3.Session, stack: DeployedStack) -> list[CheckResult]:
    name = f"DynamoDB {CONTACT_SORT_KEY}"
    contact_row = read_contact_row(session, stack)
    if contact_row is None:
        return [CheckResult(CheckStatus.FAIL, name, "missing; run scripts/seed_graph.py")]
    whatsapp_number = contact_row.get("contactWhatsApp")
    verified_via = contact_row.get("verifiedVia")
    problems = [] if whatsapp_number else ["no contactWhatsApp"]
    if verified_via not in set(VerifiedVia):
        problems.append(f"verifiedVia {verified_via!r} is not one of {[str(value) for value in VerifiedVia]}")
    detail = (f"verifiedVia={verified_via} verifiedAt={contact_row.get('verifiedAt')} "
              f"contactWhatsApp={mask_phone_number(whatsapp_number) if whatsapp_number else 'none'} "
              f"knownAddresses={len(contact_row.get('knownAddresses', []))}")
    return [CheckResult(CheckStatus.FAIL if problems else CheckStatus.OK, name, "; ".join([detail, *problems]))]


def deployed_flag_result(function_label: str, environment: dict[str, str], variable: str, context_key: str | None) -> CheckResult:
    """FAIL when unset or when it differs from cdk.json (redeploy); MOCK for CONTACT_VERIFICATION_MOCK=true; else OK."""
    name = f"{function_label} {variable}"
    if variable not in environment:
        return CheckResult(CheckStatus.FAIL, name, "not set on the deployed function (redeploy)")
    deployed_value = environment[variable]
    if context_key is not None:
        expected_value = "true" if CDK_CONTEXT.get(context_key) is True else "false"
        if deployed_value != expected_value:
            return CheckResult(CheckStatus.FAIL, name, f"deployed {deployed_value}, cdk.json {context_key} gives {expected_value} (redeploy)")
    is_mock = variable == "CONTACT_VERIFICATION_MOCK" and deployed_value == "true"
    return CheckResult(CheckStatus.MOCK if is_mock else CheckStatus.OK, name, deployed_value)


def check_deployed_flags(stack: DeployedStack) -> list[CheckResult]:
    reader_environment = stack.function_environment("ReaderFunction")
    verdict_environment = stack.function_environment("VerdictFunction")
    return [
        deployed_flag_result("pidge-reader", reader_environment, "ASKS_PER_CONTACT_PER_HOUR", None),
        deployed_flag_result("pidge-verdict", verdict_environment, "CHAT_ENABLED", "chatEnabled"),
        deployed_flag_result("pidge-verdict", verdict_environment, "CONTACT_VERIFICATION_MOCK", "contactVerificationMock"),
    ]


def main() -> None:
    session = boto3.Session(region_name=REGION_NAME)
    stack = DeployedStack(session)
    checks: list[tuple[str, Callable[[], list[CheckResult]]]] = [
        ("Bedrock models", lambda: check_bedrock_models(session, stack)),
        ("WhatsApp template", lambda: check_whatsapp_template(session)),
        ("SNS subscriptions", lambda: check_sns_subscriptions(session, stack)),
        ("RCS tester", check_rcs_tester),
        ("MX", lambda: check_receiver_mx(stack)),
        ("SES identities", lambda: check_ses_identities(session, stack)),
        ("DynamoDB contact", lambda: check_contact_row(session, stack)),
        ("Deployed flags", lambda: check_deployed_flags(stack)),
    ]
    results = [result for name, check_function in checks for result in run_check(name, check_function)]
    for result in results:
        print(f"{result.status:<4} {result.name}: {result.detail}")
    sys.exit(1 if any(result.status is CheckStatus.FAIL for result in results) else 0)


if __name__ == "__main__":
    main()
