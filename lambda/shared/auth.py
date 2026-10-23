"""Reads the X-Pidge-Auth header that Mail Manager stamps at ingress."""

from collections.abc import Sequence

from shared.enums import ReasonCode

PASS_VALUE = "pass"
MECHANISMS_THAT_VOUCH = ("spf", "dkim")  # Mail Manager stamps these two; DMARC is not a verdict it exposes


def parse_authentication_header(header_value: str) -> dict[str, str]:
    """'spf=pass;dkim=fail' -> {'spf': 'pass', 'dkim': 'fail'}."""
    verdicts: dict[str, str] = {}
    for pair in header_value.split(";"):
        mechanism, separator, result = pair.strip().partition("=")
        if separator:
            verdicts[mechanism.strip().lower()] = result.strip().lower()
    return verdicts


def authentication_reasons(pidge_auth_header_values: Sequence[str]) -> list[ReasonCode]:
    """Exactly one header is the only clean shape; anything else is a reason."""
    header_count = len(pidge_auth_header_values)
    if header_count == 0:
        return [ReasonCode.AUTH_MISSING]
    if header_count > 1:
        return [ReasonCode.AUTH_TAMPERED]
    verdicts = parse_authentication_header(pidge_auth_header_values[0])
    if not any(verdicts.get(mechanism) == PASS_VALUE for mechanism in MECHANISMS_THAT_VOUCH):
        return [ReasonCode.AUTH_FAILED]
    return []
