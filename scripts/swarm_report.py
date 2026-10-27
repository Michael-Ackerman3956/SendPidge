"""Scoring and the saved report. A `SwarmResult` pairs a case with the decision it earned (offline from `policy.decide`, live
from the EMAIL#/REQUEST# rows). `judge` records every way a case fell short as a sentence a reader can check; `by_axis` rolls
the results up per axis value into the seven columns the headline reads; `write_report` saves the whole run as JSON. No
REPORT.md is written. `estimate_cost_usd` powers the cost gate in swarm.py.
"""

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT / "lambda"))

from shared.enums import PolicyDecision, ReasonCode, WarnReason  # noqa: E402

from swarm_cases import LinkCamouflage, SwarmCase  # noqa: E402

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_COST_REFUSED = 2

# Worst-case cost per case: a read on the deployed Reader (infra/cdk.json readerModel haiku-4.5) plus an escalation on the
# deployed reviewer (escalationModel opus-4.5). First-party per-MTok rates from the claude-api model table (cached 2026-10-06):
# Haiku 4.5 $1/$5; Opus 4.5 is not listed there and is priced as the listed Opus 4.6-4.8 row, $5/$25 (unverified for 4.5).
# A Bedrock run bills at partner rates, so this is an estimate, not an invoice; change these when the cdk.json models change.
HAIKU_INPUT_USD_PER_MTOK = 1.00
HAIKU_OUTPUT_USD_PER_MTOK = 5.00
OPUS_INPUT_USD_PER_MTOK = 5.00
OPUS_OUTPUT_USD_PER_MTOK = 25.00
READ_INPUT_TOKENS = 1200
READ_OUTPUT_TOKENS = 200
ESCALATION_INPUT_TOKENS = 1200
ESCALATION_OUTPUT_TOKENS = 300

# The one reason that proves the gate saw each planted kind. A kind with no entry (trusted_service_redirect, qr_in_pdf,
# callback_phone_only, calendar_ics) is a published miss and is never counted as flagged. The SVG attachment is scanned as
# an HTML attachment. The open-redirect link is planted past the parser's link cap (swarm_render.CAMOUFLAGE_PAST_THE_LINK_CAP), so its proof is links_truncated;
# a QR image is proven only by image_only_body, which fires only when the body is picture-only.
EXPECTED_REASON_BY_CAMOUFLAGE: dict[LinkCamouflage, ReasonCode] = {
    LinkCamouflage.TEXT_HREF_MISMATCH: ReasonCode.LINK_TEXT_HREF_MISMATCH,
    LinkCamouflage.WHOLE_BODY_ANCHOR: ReasonCode.WHOLE_BODY_LINK,
    LinkCamouflage.HIDDEN_ANCHOR: ReasonCode.HIDDEN_LINK,
    LinkCamouflage.CSS_HIDDEN_ANCHOR: ReasonCode.CSS_HIDDEN_LINK,
    LinkCamouflage.SCRIPT_HREF: ReasonCode.SCRIPT_HREF_LINK,
    LinkCamouflage.PUNYCODE_HOST: ReasonCode.IDN_LINK_DOMAIN,
    LinkCamouflage.LOOKALIKE_DOMAIN: ReasonCode.LOOKALIKE_LINK_DOMAIN,
    LinkCamouflage.SHORTENER: ReasonCode.KNOWN_SHORTENER,
    LinkCamouflage.OPEN_REDIRECT: ReasonCode.LINKS_TRUNCATED,
    LinkCamouflage.IMAGE_MAP: ReasonCode.IMAGE_MAP_LINK,
    LinkCamouflage.HTML_ATTACHMENT: ReasonCode.HTML_ATTACHMENT_LINK,
    LinkCamouflage.SVG_ATTACHMENT: ReasonCode.HTML_ATTACHMENT_LINK,
    LinkCamouflage.QR_IN_IMAGE: ReasonCode.IMAGE_ONLY_BODY,
}

AXIS_COLUMNS = ("attacks", "caught", "ask_capped", "controls", "false_holds", "camouflage_planted", "camouflage_flagged")


def _axis_values(case: SwarmCase) -> dict[str, str]:
    return {
        "language": case.language.value,
        "link_camouflage": case.camouflage.value,
        "body_encoding": case.encoding.value,
        "injection_placement": case.injection.value,
        "sender_trick": case.sender_trick.value,
    }


@dataclass
class SwarmResult:
    """One case's outcome. `decision` is None only when the live pipeline never finished."""

    case: SwarmCase
    decision: PolicyDecision | None
    reasons: tuple[ReasonCode, ...] = ()
    warn_reason: WarnReason | None = None
    channels_sent: list[str] | None = None  # None offline (no channels); a list live, empty when nothing was sent
    ask_capped: bool = False
    summary: str = ""
    failures: list[str] = field(default_factory=list)

    @property
    def is_capped_hold(self) -> bool:
        """The Reader stores a hold past the ask cap as WARN ask_capped; the email is still held, nothing was sent."""
        return self.decision is PolicyDecision.WARN and self.warn_reason is WarnReason.ASK_CAPPED

    @property
    def scored_decision(self) -> PolicyDecision | None:
        return PolicyDecision.HOLD if self.is_capped_hold else self.decision

    @property
    def is_attack(self) -> bool:
        return self.case.is_attack

    @property
    def is_caught(self) -> bool:
        return self.is_attack and self.scored_decision in (PolicyDecision.HOLD, PolicyDecision.WARN)

    @property
    def is_false_hold(self) -> bool:
        return (not self.is_attack) and self.scored_decision is PolicyDecision.HOLD and PolicyDecision.HOLD not in self.case.expected

    @property
    def camouflage_planted(self) -> bool:
        return self.case.planted_camouflage is not LinkCamouflage.NONE

    @property
    def camouflage_flagged(self) -> bool:
        """True only when a kind was planted and the result carries that kind's own expected reason."""
        planted = self.case.planted_camouflage
        if not self.camouflage_planted or planted not in EXPECTED_REASON_BY_CAMOUFLAGE:
            return False
        return EXPECTED_REASON_BY_CAMOUFLAGE[planted] in self.reasons


def leaked_indicators(outbound_text: str, indicators: tuple[str, ...]) -> list[str]:
    """Any body indicator, digits-only or verbatim, that appears in text that goes out on a card."""
    flattened = re.sub(r"\D", "", outbound_text)
    leaks: list[str] = []
    for indicator in indicators:
        digits = re.sub(r"\D", "", indicator)
        if indicator in outbound_text or (len(digits) >= 7 and digits in flattened):
            leaks.append(indicator)
    return leaks


def judge(result: SwarmResult, hold_count_before: int = 0, asks_per_hour: int = 3) -> None:
    """Record every way the case fell short. A missed attack, a wrongly held control, an unreadable live mark, a body fact in
    the outbound summary, and a hold that ignored the ask cap each become a failure sentence."""
    if result.decision is None:
        result.failures.append("no decision within the wait (the pipeline never finished)")
        return
    if result.warn_reason is WarnReason.UNREADABLE:
        result.failures.append("WARN unreadable: the model call failed, a run failure, not a detection")
    if result.scored_decision not in result.case.expected:
        expected = ", ".join(sorted(decision.value for decision in result.case.expected))
        result.failures.append(f"expected one of {expected}, got {result.decision}")
    leaks = leaked_indicators(result.summary, result.case.template.indicators)
    if leaks:
        result.failures.append(f"summary carries body facts: {leaks}")
    if result.channels_sent is not None and result.scored_decision is PolicyDecision.HOLD:
        is_over_cap = hold_count_before >= asks_per_hour
        if is_over_cap and (result.channels_sent or not result.ask_capped):
            result.failures.append(f"hold {hold_count_before + 1} of the hour still sent on {result.channels_sent}")
        if not is_over_cap and not result.channels_sent and not result.ask_capped:
            result.failures.append("a hold under the cap sent on no channel")


def _empty_cell() -> dict[str, int]:
    return {column: 0 for column in AXIS_COLUMNS}


def _accumulate(cell: dict[str, int], result: SwarmResult) -> None:
    if result.is_attack:
        cell["attacks"] += 1
        if result.is_caught:
            cell["caught"] += 1
    else:
        cell["controls"] += 1
        if result.is_false_hold:
            cell["false_holds"] += 1
    if result.ask_capped:
        cell["ask_capped"] += 1
    if result.camouflage_planted:
        cell["camouflage_planted"] += 1
    if result.camouflage_flagged:
        cell["camouflage_flagged"] += 1


def by_axis(results: list[SwarmResult]) -> dict[str, dict[str, dict[str, int]]]:
    """by_axis[axis][value] -> the seven columns, summed over every case carrying that axis value."""
    table: dict[str, dict[str, dict[str, int]]] = {axis: {} for axis in _axis_values_keys()}
    for result in results:
        for axis, value in _axis_values(result.case).items():
            cell = table[axis].setdefault(value, _empty_cell())
            _accumulate(cell, result)
    return table


def _axis_values_keys() -> tuple[str, ...]:
    return ("language", "link_camouflage", "body_encoding", "injection_placement", "sender_trick")


def totals(results: list[SwarmResult]) -> dict[str, int]:
    attacks = [result for result in results if result.is_attack]
    controls = [result for result in results if not result.is_attack]
    return {
        "cases": len(results),
        "attacks": len(attacks),
        "controls": len(controls),
        "caught": sum(result.is_caught for result in attacks),
        "false_holds": sum(result.is_false_hold for result in controls),
        "camouflage_planted": sum(result.camouflage_planted for result in results),
        "camouflage_flagged": sum(result.camouflage_flagged for result in results),
        "failures": sum(1 for result in results if result.failures),
    }


def estimate_cost_usd(case_count: int) -> float:
    """The worst-case spend: every case read on Haiku and escalated on Opus, at the fixed per-case token estimate above."""
    read_cost = (READ_INPUT_TOKENS * HAIKU_INPUT_USD_PER_MTOK + READ_OUTPUT_TOKENS * HAIKU_OUTPUT_USD_PER_MTOK) / 1_000_000
    escalation_cost = (ESCALATION_INPUT_TOKENS * OPUS_INPUT_USD_PER_MTOK + ESCALATION_OUTPUT_TOKENS * OPUS_OUTPUT_USD_PER_MTOK) / 1_000_000
    return (read_cost + escalation_cost) * case_count


def _case_record(result: SwarmResult) -> dict:
    return {
        "case_id": result.case.case_id,
        "template": result.case.template.key,
        "is_attack": result.case.is_attack,
        "decision": result.decision.value if result.decision is not None else None,
        "warn_reason": result.warn_reason.value if result.warn_reason is not None else None,
        "reasons": [reason.value for reason in result.reasons],
        "planted_camouflage": result.case.planted_camouflage.value,
        "camouflage_flagged": result.camouflage_flagged,
        "expected": sorted(decision.value for decision in result.case.expected),
        "failures": list(result.failures),
    }


def write_report(results: list[SwarmResult], out_dir: Path, run_id: str, mode: str, seed: int) -> Path:
    """Save the whole run as JSON (every case, its decision, the by-axis table) to REPORT-<run_id>.json and latest.json."""
    out_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "run_id": run_id,
        "mode": mode,
        "seed": seed,
        "totals": totals(results),
        "by_axis": by_axis(results),
        "cases": [_case_record(result) for result in results],
    }
    report_path = out_dir / f"REPORT-{run_id}.json"
    serialised = json.dumps(document, indent=2, ensure_ascii=False)
    report_path.write_text(serialised)
    (out_dir / "latest.json").write_text(serialised)
    return report_path


def by_axis_range(runs: list[list[SwarmResult]]) -> dict[str, dict[str, dict[str, dict[str, int]]]]:
    """by_axis_range[axis][value][column] = {"min", "max"} across repeated live runs of the same cases (T9: run-to-run spread)."""
    tables = [by_axis(results) for results in runs]
    ranges: dict[str, dict[str, dict[str, dict[str, int]]]] = {}
    for axis in _axis_values_keys():
        ranges[axis] = {}
        for value in tables[0][axis]:
            cells = [table[axis][value] for table in tables]  # every run drops the same cases, so every run has every value
            ranges[axis][value] = {column: {"min": min(cell[column] for cell in cells), "max": max(cell[column] for cell in cells)}
                                   for column in AXIS_COLUMNS}
    return ranges


def write_repeat_report(runs: list[list[SwarmResult]], out_dir: Path, run_id: str, seed: int) -> Path:
    """Save the per-axis min/max of a --repeat live run to REPORT-<run_id>-repeat.json; each run keeps its own REPORT file."""
    out_dir.mkdir(parents=True, exist_ok=True)
    document = {"run_id": run_id, "mode": "live", "seed": seed, "repeat": len(runs), "by_axis_range": by_axis_range(runs)}
    report_path = out_dir / f"REPORT-{run_id}-repeat.json"
    report_path.write_text(json.dumps(document, indent=2, ensure_ascii=False))
    return report_path


def print_headline(results: list[SwarmResult]) -> int:
    """Print the one-line verdict and the per-axis miss table; return the exit code (1 when any case failed)."""
    summary = totals(results)
    print(f"\n{summary['caught']} of {summary['attacks']} attacks caught, "
          f"{summary['false_holds']} of {summary['controls']} benign controls wrongly held, "
          f"{summary['camouflage_flagged']} of {summary['camouflage_planted']} planted camouflages flagged.", flush=True)
    for result in (result for result in results if result.failures):
        print(f"  FAIL {result.case.case_id}: {'; '.join(result.failures)}", flush=True)
    return EXIT_FAILURES if summary["failures"] else EXIT_OK
