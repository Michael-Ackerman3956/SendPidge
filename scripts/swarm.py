"""The attacker swarm: 100+ generated attack emails and a third benign controls through the pipeline, then a per-axis report.

Three modes (`SwarmMode`):
  dry_run  write the .eml files and send nothing.
  offline  parse -> links -> lists -> policy.decide with each template's scripted verdict, no model, no AWS.
  live     drop each case under inbox/ in the email bucket and invoke the Reader (CDK output ReaderFunctionName) with
           the event Mail Manager sends it, envelope recipient --receiver-address (an enrolled resident), one at a time,
           then read the EMAIL#/REQUEST# rows. A live run proves detection on every case; the ask cap
           (lambda/shared/pipeline.py hold_and_ask) means a hold fans out on channels at most three times per UTC
           clock hour, so the swarm's hold count restarts when the hour changes.

The live run estimates its worst-case spend (every case read on Haiku and escalated on Opus) and refuses before the first
drop when it exceeds --max-cost-usd. Point live mode at an in-app-only contact, or every hold sends a real card.

Usage:
  python scripts/swarm.py --generate --seed 7                 # offline, writes docs/swarm/REPORT-<run_id>.json
  python scripts/swarm.py --mode dry_run --out-dir /tmp/swarm # only writes the .eml files
  AWS_PROFILE=sendpidge python scripts/swarm.py --mode live --bucket <email bucket>
"""

import argparse
import json
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT / "lambda"))

from shared import graph  # noqa: E402
from shared.auth import authentication_reasons  # noqa: E402
from shared.email_parse import parse_eml  # noqa: E402
from shared.enums import PolicyDecision  # noqa: E402
from shared.graph import Contact, domains_of, find_contact_among  # noqa: E402
from shared.hourly_counters import current_hour_label  # noqa: E402
from shared.links import assess_links, link_reasons  # noqa: E402
from shared.lists import SeedListStore, list_row_from_seed, sender_reasons_or_lookup_failure  # noqa: E402
from shared.policy import decide  # noqa: E402

import swarm_report  # noqa: E402
from swarm_cases import CONTACT_KNOWN_ADDRESS, CONTACT_NAME, SwarmMode, generate_cases  # noqa: E402
from swarm_render import build_case_eml  # noqa: E402
from swarm_report import EXIT_COST_REFUSED, SwarmResult, estimate_cost_usd, judge, print_headline  # noqa: E402

DEFAULT_SEED = 7
DEFAULT_OUT_DIR = REPOSITORY_ROOT / "docs" / "swarm"
DEFAULT_LISTS = REPOSITORY_ROOT / "infra" / "seed" / "lists.sample.json"
TABLE_NAME = "sendpidge"
INBOX_PREFIX = "inbox/"
DEFAULT_DELAY_SECONDS = 8
DEFAULT_WAIT_SECONDS = 90
DEFAULT_ASKS_PER_HOUR = 3
READER_FUNCTION_NAME_OUTPUT_NAME = "ReaderFunctionName"


@dataclass(frozen=True)
class LiveMailDrop:
    """Stands in for Mail Manager in a live run: stores the case under inbox/ (store-raw-email's write), then invokes the Reader
    asynchronously with the same event shape the rule's InvokeLambda action sends (lambda/pidge_reader/handler.py)."""
    s3_client: object
    lambda_client: object
    reader_function_name: str

    def deliver(self, bucket: str, message_id: str, raw_email: bytes, receiver_address: str) -> None:
        self.s3_client.put_object(Bucket=bucket, Key=f"{INBOX_PREFIX}{message_id}", Body=raw_email)
        event = {"Records": [{"eventSource": "aws:ses",
                              "ses": {"mail": {"messageId": message_id}, "receipt": {"recipients": [receiver_address]}}}]}
        self.lambda_client.invoke(FunctionName=self.reader_function_name, InvocationType="Event",
                                  Payload=json.dumps(event).encode())


def build_contacts() -> list[Contact]:
    return [Contact(f"CONTACT#{CONTACT_NAME.casefold()}", CONTACT_NAME, CONTACT_NAME.casefold(),
                    known_addresses=(CONTACT_KNOWN_ADDRESS,))]


def load_list_store(lists_path: Path) -> SeedListStore:
    rows = json.loads(Path(lists_path).read_text())
    return SeedListStore([list_row_from_seed(row) for row in rows])


def offline_result(case, contacts: list[Contact], family_domains, list_store: SeedListStore) -> SwarmResult:
    """Run one case through the production gate functions with its scripted verdict standing in for the model."""
    parsed = parse_eml(build_case_eml(case))
    reasons = authentication_reasons(parsed.pidge_auth_header_values)
    reasons.extend(link_reasons(assess_links(parsed.links, family_domains), parsed))
    reasons.extend(sender_reasons_or_lookup_failure(list_store, parsed.sender_address, parsed.sender_domain,
                                                    parsed.sender_name, contacts))
    verdict = case.scripted_verdict
    contact_by_name = find_contact_among(contacts, parsed.sender_name)
    contact_by_claim = find_contact_among(contacts, verdict.claimed_person)
    outcome = decide(verdict, contact_by_name, contact_by_claim, parsed.sender_address, tuple(reasons))
    result = SwarmResult(case, outcome.decision, tuple(outcome.reasons), outcome.warn_reason, summary=verdict.summary)
    judge(result)
    return result


def run_offline(cases, arguments) -> int:
    contacts = build_contacts()
    family_domains = domains_of(contacts)
    list_store = load_list_store(arguments.lists)
    results = [offline_result(case, contacts, family_domains, list_store) for case in cases]
    run_id = uuid.uuid4().hex[:8]
    saved = swarm_report.write_report(results, Path(arguments.out_dir), run_id, SwarmMode.OFFLINE.value, arguments.seed)
    print(f"offline: {len(cases)} cases, report {saved}", flush=True)
    return print_headline(results)


def run_dry(cases, arguments) -> int:
    # --dry-run DIRECTORY names the directory; a bare --dry-run or --mode dry_run writes to --out-dir.
    output_directory = Path(arguments.dry_run if isinstance(arguments.dry_run, str) else arguments.out_dir)
    output_directory.mkdir(parents=True, exist_ok=True)
    for index, case in enumerate(cases, start=1):
        (output_directory / f"{index:04d}-{case.case_id.replace('·', '-')}.eml").write_bytes(build_case_eml(case))
    print(f"dry_run: wrote {len(cases)} emails to {output_directory}", flush=True)
    return swarm_report.EXIT_OK


def wait_for_email_row(table, object_key: str, wait_seconds: int) -> dict | None:
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        row = table.get_item(Key={"PK": f"EMAIL#{object_key}", "SK": graph.METADATA_SORT_KEY}).get("Item")
        if row is not None and "state" in row:
            return row
        time.sleep(3)
    return None


def read_live_result(case, table, object_key: str, wait_seconds: int) -> SwarmResult:
    """Read the deployed pipeline's decision for one dropped case; a missing required field raises, never a silent default."""
    from shared.enums import ReasonCode, WarnReason
    email_row = wait_for_email_row(table, object_key, wait_seconds)
    if email_row is None:
        return SwarmResult(case, None)
    reasons = tuple(ReasonCode(value) for value in email_row["reasons"])
    warn_reason = WarnReason(email_row["warnReason"]) if email_row["warnReason"] else None
    result = SwarmResult(case, PolicyDecision(email_row["state"]), reasons, warn_reason)
    if email_row["requestId"] is None:
        return result
    request_row = table.get_item(Key={"PK": f"REQUEST#{email_row['requestId']}", "SK": graph.METADATA_SORT_KEY}).get("Item")
    if request_row is None:
        raise RuntimeError(f"{object_key} names request {email_row['requestId']}, which has no row")
    result.channels_sent = list(request_row["channelsSent"])
    result.ask_capped = "askCapped" in request_row and bool(request_row["askCapped"])  # written only when the cap stopped the ask
    result.summary = str(request_row["verdict"]["summary"])
    return result


def is_over_budget(case_count: int, arguments) -> bool:
    """The worst case for every --repeat run, checked once before the first put_object; a run is never stopped mid-way."""
    estimate = estimate_cost_usd(case_count) * arguments.repeat
    if estimate <= arguments.max_cost_usd:
        return False
    print(f"refusing: worst-case estimate ${estimate:.2f} for {arguments.repeat} run(s) exceeds --max-cost-usd "
          f"${arguments.max_cost_usd:.2f}; nothing was dropped", flush=True)
    return True


def run_live(cases, mail_drop: LiveMailDrop, table, arguments) -> int:
    """One cost check, then --repeat live runs; each writes its report, and a repeated run adds the per-axis min/max.
    Returns the worst exit code across the runs."""
    if is_over_budget(len(cases), arguments):
        return EXIT_COST_REFUSED
    runs: list[list[SwarmResult]] = []
    exit_codes: list[int] = []
    for repeat_index in range(arguments.repeat):
        print(f"=== live run {repeat_index + 1} of {arguments.repeat} ===", flush=True)
        runs.append(run_live_once(cases, mail_drop, table, arguments))
        exit_codes.append(print_headline(runs[-1]))
    if len(runs) > 1:
        saved = swarm_report.write_repeat_report(runs, Path(arguments.out_dir), uuid.uuid4().hex[:6], arguments.seed)
        print(f"live: per-axis min/max over {len(runs)} runs in {saved}", flush=True)
    return max(exit_codes)


def holds_counted_for_hour(holds_counted: int, counted_hour_label: str, drop_hour_label: str) -> int:
    """The holds already asked in the drop's UTC hour: the running count while the hour is the same, zero once it changed
    (the Reader's ask counter is one row per current_hour_label, so a new hour starts a fresh cap)."""
    return holds_counted if drop_hour_label == counted_hour_label else 0


def run_live_once(cases, mail_drop: LiveMailDrop, table, arguments) -> list[SwarmResult]:
    """Drop each case, read its decision and write this run's report. The hold count judged against the ask cap is per
    UTC clock hour, starting from --holds-already-this-hour."""
    run_id = uuid.uuid4().hex[:6]
    results: list[SwarmResult] = []
    holds_so_far = arguments.holds_already_this_hour
    counted_hour_label = current_hour_label()
    for index, case in enumerate(cases, start=1):
        drop_hour_label = current_hour_label()
        holds_so_far = holds_counted_for_hour(holds_so_far, counted_hour_label, drop_hour_label)
        counted_hour_label = drop_hour_label
        message_id = f"swarm-{run_id}-{index:04d}-{case.template.key}.eml"
        object_key = f"{INBOX_PREFIX}{message_id}"
        mail_drop.deliver(arguments.bucket, message_id, build_case_eml(case), arguments.receiver_address)
        result = read_live_result(case, table, object_key, arguments.wait_seconds)
        judge(result, holds_so_far, arguments.asks_per_hour)
        if result.scored_decision is PolicyDecision.HOLD:
            holds_so_far += 1
        results.append(result)
        print(f"[{index:04d}] {case.case_id} -> {result.decision} {result.warn_reason or ''}"
              + (f"  FAIL: {result.failures}" if result.failures else ""), flush=True)
        time.sleep(arguments.delay_seconds)
    saved = swarm_report.write_report(results, Path(arguments.out_dir), run_id, SwarmMode.LIVE.value, arguments.seed)
    print(f"live: report {saved}", flush=True)
    return results


def open_live_clients(arguments) -> tuple[LiveMailDrop, object]:
    import boto3  # only the live run talks to AWS
    from enrol_resident import read_stack_outputs

    session = boto3.Session(region_name=arguments.region)
    reader_function_name = read_stack_outputs(session)[READER_FUNCTION_NAME_OUTPUT_NAME]
    mail_drop = LiveMailDrop(session.client("s3"), session.client("lambda"), reader_function_name)
    return mail_drop, session.resource("dynamodb").Table(arguments.table)


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", type=SwarmMode, choices=list(SwarmMode), default=SwarmMode.OFFLINE)
    parser.add_argument("--generate", action="store_true", help="shorthand for the offline run")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="where REPORT-<run_id>.json and latest.json are written")
    parser.add_argument("--lists", default=str(DEFAULT_LISTS), help="the scam-list seed file read in memory for offline mode")
    parser.add_argument("--max-cost-usd", type=float, default=10.0, help="refuse a live run whose worst-case estimate is higher")
    parser.add_argument("--repeat", type=int, default=1,
                        help="re-run live this many times: the worst exit, and a per-axis min/max report")
    parser.add_argument("--dry-run", nargs="?", const=True, default=None, metavar="DIRECTORY",
                        help="write the .eml files (here, or to --out-dir) and send nothing")
    parser.add_argument("--bucket", help="the email bucket (CDK output EmailBucketName); required for a live run")
    parser.add_argument("--table", default=TABLE_NAME)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--receiver-address", default="grandma@sendpidge.com",
                        help="the enrolled resident the live run delivers to (the envelope recipient)")
    parser.add_argument("--asks-per-hour", type=int, default=DEFAULT_ASKS_PER_HOUR, help="the deployed ASKS_PER_CONTACT_PER_HOUR")
    parser.add_argument("--holds-already-this-hour", type=int, default=0, help="asks the contact already got this hour")
    parser.add_argument("--delay-seconds", type=int, default=DEFAULT_DELAY_SECONDS)
    parser.add_argument("--wait-seconds", type=int, default=DEFAULT_WAIT_SECONDS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(sys.argv[1:] if argv is None else argv)
    cases = generate_cases(arguments.seed)
    if arguments.dry_run is not None:
        return run_dry(cases, arguments)
    if arguments.generate or arguments.mode is SwarmMode.OFFLINE:
        return run_offline(cases, arguments)
    if arguments.mode is SwarmMode.DRY_RUN:
        return run_dry(cases, arguments)
    if not arguments.bucket:
        raise SystemExit("--bucket is required for a live run (or use --mode dry_run / --generate)")
    mail_drop, table = open_live_clients(arguments)
    return run_live(cases, mail_drop, table, arguments)


if __name__ == "__main__":
    sys.exit(main())
