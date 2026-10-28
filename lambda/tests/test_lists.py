"""The scam list: normalisers, lookalikes, LIST# reads that add reasons (never a verdict), a failed read that marks, the
ask cap that fails closed, public-feed rows, check() reading a stale feed or a failed read as unavailable, never clean,
and a listed link domain marking the link LINK_LISTED. Reads go through the production table fixture."""

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from shared import graph, hourly_counters, inbox_rows, lists
from shared.email_parse import EmailLink, link_kind_of_href, parse_eml
from shared.enums import (
    Channel, Friction, HiddenBy, IndicatorKind, LinkSource, ListSource, PolicyDecision, ReasonCode, RequestState, WarnReason,
)
from shared.links import assess_links, link_reasons, list_indicators_of, mark_listed_links
from shared.list_enums import ScamListCoverageState
from shared.lists import (
    LIST_CHECK_FAILED_METRIC_NAME, DynamoListStore, Indicator, ListLookupFailed, SeedListStore, canonical_url,
    check, confusable_skeleton, indicator_key, is_lookalike_name, list_row_from_seed, lookup_sender_reasons,
    normalise_phone_e164, normalise_registrable_domain, parse_list_row, public_list_row_from_seed,
    sender_reasons_or_lookup_failure,
)
from shared.policy import decide
from shared.reader_agent import EmailVerdict
from shared.enums import Intent
from shared.sources import check_against_live_sources, source_row_from_seed
from tests.test_sources import AMF, PHISHING_DATABASE, put_source, seed_source
from tests.test_verdict import aws  # noqa: F401

SEED_PATH = Path(__file__).resolve().parents[2] / "infra" / "seed" / "lists.sample.json"
PUBLIC_SEED_PATH = SEED_PATH.parent / "lists.public.json"
SAMPLES_PATH = Path(__file__).resolve().parents[2] / "samples"
NOW = "2026-10-27T15:00:00Z"
KISA_2024 = "kr-kisa-phishing-2024"
AMF_ATTRIBUTION = "AMF Listes noires · Licence Ouverte 2.0"
JIMIN = graph.Contact("CONTACT#jimin", "Jimin", "jimin", known_addresses=("jimin@example.com",))
MINHO = graph.Contact("CONTACT#minho", "Minho", "minho", known_addresses=("minho@family.example",))
CONTACTS = [JIMIN, MINHO]


def seed_rows() -> list[dict]:
    return [list_row_from_seed(seed) for seed in json.loads(SEED_PATH.read_text())]


def seeded_store() -> SeedListStore:
    return SeedListStore(seed_rows())


def public_seeds() -> list[dict]:
    return json.loads(PUBLIC_SEED_PATH.read_text(), parse_float=Decimal)


def public_seed(value: str) -> dict:
    return next(seed for seed in public_seeds() if seed["value"] == value)


def test_normalisers():
    assert normalise_registrable_domain("Mail.Jimin-Help.Example.") == "jimin-help.example"
    assert normalise_registrable_domain("news.bbc.co.uk") == "bbc.co.uk"
    assert normalise_phone_e164("+82 10-0000-0001") == "+821000000001"
    assert normalise_phone_e164("0082 10 0000 0001") == "+821000000001"
    with pytest.raises(ValueError):
        normalise_phone_e164("call me")


def test_skeleton_and_lookalike_names():
    assert confusable_skeleton("Jіmin") == confusable_skeleton("Jimin")  # Cyrillic і
    assert confusable_skeleton("PayPa1.com") == "paypal.com"
    assert is_lookalike_name("Jimln", "Jimin")       # one edit
    assert is_lookalike_name("Jіmin", "Jimin")       # confusable
    assert is_lookalike_name("J i m i n", "Jimin")   # spacing
    assert not is_lookalike_name("JImin", "Jimin")   # exact once case-folded: gate 1 owns it
    assert not is_lookalike_name("Jim", "Kim")       # short names are never one-edit lookalikes
    assert not is_lookalike_name("Grandpa", "Jimin")


def test_every_seed_row_parses_and_has_the_full_shape():
    rows = seed_rows()
    assert len(rows) >= 20
    for row in rows:
        parsed = parse_list_row(row)
        assert parsed.source is ListSource.SAMPLE
        assert {"category", "strength", "source", "tier", "reason", "attribution", "addedAt", "expiresAt", "generation"} <= set(row)
    assert {row["PK"] for row in rows} == {"LIST#domain", "LIST#address", "LIST#lookalike", "LIST#phone", "LIST#url"}
    assert any(row["SK"] == "+821000000001" for row in rows if row["PK"] == "LIST#phone")


@pytest.mark.parametrize(
    ("sender_name", "sender_address", "expected_reasons"),
    [
        ("Jimin", "jimin.kim@jimin-help.example", [ReasonCode.LISTED_SENDER_DOMAIN]),
        ("Grandma's Bank", "support@kakaobank-secure.example", [ReasonCode.LISTED_SENDER_ADDRESS, ReasonCode.LISTED_SENDER_DOMAIN]),
        ("Jimln", "someone@freemail.example", [ReasonCode.LOOKALIKE_SENDER_NAME]),
        ("Jіmin", "someone@freemail.example", [ReasonCode.LOOKALIKE_SENDER_NAME]),
        ("Jimln", "jimin@example.com", []),  # a near-match from the contact's own known address is not a lookalike
        ("Jimin", "jimin@example.com", []),  # the real thing
        ("Minho", "minho@family-example.example", [ReasonCode.LOOKALIKE_SENDER_DOMAIN]),
        ("Newsletter", "news@shop.example", []),
    ],
)
def test_lookup_turns_rows_and_near_matches_into_reasons(sender_name, sender_address, expected_reasons):
    domain = sender_address.rpartition("@")[2]
    assert lookup_sender_reasons(seeded_store(), sender_address, domain, sender_name, CONTACTS) == expected_reasons


def test_a_confirm_row_is_reported_sender_and_a_read_failure_marks(aws, monkeypatch):  # noqa: F811
    graph.write_listed_address("Scammer@Lookalike.example", "Reported after the real contact said they did not send it", "req-test")
    stored = graph.get_table().get_item(Key={"PK": "LIST#address", "SK": "scammer@lookalike.example"})["Item"]
    assert parse_list_row(stored).source is ListSource.PIDGE_CONFIRMED and stored["tier"] == 0
    store = DynamoListStore()
    assert lookup_sender_reasons(store, "scammer@lookalike.example", "lookalike.example", "Someone", CONTACTS) == [ReasonCode.REPORTED_SENDER]
    assert graph.delete_listed_address("scammer@lookalike.example", "req-test")
    assert lookup_sender_reasons(store, "scammer@lookalike.example", "lookalike.example", "Someone", CONTACTS) == []

    def failing_get_item(**_):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow"}}, "GetItem")

    monkeypatch.setattr(graph.get_table(), "get_item", failing_get_item)
    with pytest.raises(ListLookupFailed):
        lookup_sender_reasons(store, "x@y.example", "y.example", "X", CONTACTS)
    assert sender_reasons_or_lookup_failure(store, "x@y.example", "y.example", "X", CONTACTS) == [ReasonCode.LIST_LOOKUP_FAILED]


def test_list_reasons_mark_but_never_hold_on_their_own():
    quiet_verdict = EmailVerdict(claimed_person=None, intent=Intent.NONE, confidence=0.1, summary="A note.")
    marked = decide(quiet_verdict, None, None, "support@kakaobank-secure.example", [ReasonCode.LISTED_SENDER_ADDRESS])
    assert (marked.decision, marked.warn_reason) == (PolicyDecision.WARN, WarnReason.LISTED_SENDER)
    failed = decide(quiet_verdict, None, None, "x@y.example", [ReasonCode.LIST_LOOKUP_FAILED])
    assert (failed.decision, failed.warn_reason) == (PolicyDecision.WARN, WarnReason.LISTED_SENDER)
    clean = decide(quiet_verdict, None, None, "x@y.example", [])
    assert clean.decision is PolicyDecision.PASS


def test_the_ask_cap_holds_the_fourth_ask_and_fails_closed(aws, monkeypatch):  # noqa: F811
    for _ in range(3):
        assert hourly_counters.take_ask_slot("CONTACT#jimin", "grandma@example.com", 3)
    assert not hourly_counters.take_ask_slot("CONTACT#jimin", "grandma@example.com", 3)
    assert hourly_counters.take_ask_slot("CONTACT#minho", "grandma@example.com", 3)  # the cap is per contact

    def failing_update_item(**_):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow"}}, "UpdateItem")

    monkeypatch.setattr(graph.get_table(), "update_item", failing_update_item)
    assert not hourly_counters.take_ask_slot("CONTACT#other", "grandma@example.com", 3)


def test_record_ask_capped_leaves_the_request_held_with_nothing_sent(aws):  # noqa: F811
    from tests.test_verdict import new_request

    request_id = new_request()
    graph.record_ask_capped(request_id)
    request_row = graph.get_request(request_id)
    assert (request_row["channelsSent"], request_row["askCapped"], request_row["status"]) == ([], True, "pending")


def test_seed_store_and_dynamo_store_read_the_same_row(aws):  # noqa: F811
    row = next(row for row in seed_rows() if row["PK"] == "LIST#domain")
    graph.get_table().put_item(Item=row)
    assert DynamoListStore().get_row(IndicatorKind.DOMAIN, row["SK"]) == seeded_store().get_row(IndicatorKind.DOMAIN, row["SK"])


@pytest.fixture
def reader_handler(aws, monkeypatch):  # noqa: F811
    """pidge_reader.handler and its shared.pipeline with the Reader never called and every push recorded; the table is moto's."""
    import importlib

    for name, value in {"EMAIL_BUCKET": "bucket", "READER_MODEL": "haiku-4.5", "ESCALATION_MODEL": "opus-4.5",
                        "ASKS_PER_CONTACT_PER_HOUR": "3"}.items():
        monkeypatch.setenv(name, value)
    handler = importlib.import_module("pidge_reader.handler")
    importlib.reload(handler.pipeline)
    importlib.reload(handler)
    pushed = []
    monkeypatch.setattr(handler.pipeline.channels, "push_to_user", lambda user_id, message: pushed.append(message["email"]) or 1)
    return handler, pushed


def held_outcome(contact):
    from shared.policy import PolicyOutcome
    return PolicyOutcome(PolicyDecision.HOLD, None, (ReasonCode.UNKNOWN_ADDRESS,), contact=contact, friction=Friction.DOUBLE_TAP)


def test_a_capped_ask_is_stored_as_the_same_warn_the_receiver_saw(reader_handler, monkeypatch):
    """A reload must show the calm banner, not a countdown for an ask nobody received."""
    from shared.profiles import require_receiver_profile
    from tests.helpers.profiles import seed_resident
    from tests.test_verdict import EMAIL_ID, VERDICT_FIELDS, seed_contact

    resident_address = "grandma@example.com"  # the resident seed_contact files Jimin under
    handler, pushed = reader_handler
    seed_resident(resident_address, "grandma", "Grandma")
    contact = seed_contact()
    email_row = {"s3Key": EMAIL_ID, "senderName": "Jimin", "senderAddress": "x@y.example", "subject": "Hi", "receivedAt": "2026-10-25T00:00:00Z",
                 "textBody": "Send money", "quarantined": False}
    inbox_rows.claim_row(Channel.EMAIL, EMAIL_ID, email_row)
    for _ in range(3):
        assert hourly_counters.take_ask_slot(contact.contact_key, resident_address, 3)
    asked = []
    monkeypatch.setattr(handler.pipeline.channels, "ask_on_every_channel", lambda *_: asked.append(True))
    verdict = EmailVerdict(**VERDICT_FIELDS)
    handler.pipeline.hold_and_ask(require_receiver_profile(resident_address), Channel.EMAIL, EMAIL_ID, email_row, verdict,
                                  held_outcome(contact), None)

    assert asked == []
    stored = inbox_rows.get_row(Channel.EMAIL, EMAIL_ID)
    assert (stored["state"], stored["warnReason"]) == (str(PolicyDecision.WARN), str(WarnReason.ASK_CAPPED))
    request_row = graph.get_request(stored["requestId"])
    assert (request_row["status"], request_row["askCapped"]) == (str(RequestState.PENDING), True)
    assert [(email["state"], email["warnReason"]) for email in pushed] == [("hold", None), ("warn", "ask_capped")]
    assert pushed[-1]["request"]["requestId"] == stored["requestId"]


def test_url_keys_are_the_sha256_of_the_canonical_url():
    assert canonical_url("HTTPS://MarketOrbitLabs.IT.com") == "https://marketorbitlabs.it.com/"
    assert indicator_key(IndicatorKind.URL, "HTTPS://MarketOrbitLabs.IT.com") == (
        "5978deadfc1472f442da256125dc09ec65eddb9abdbf9156e3f1cbeea93c2580")
    assert canonical_url("https://bit.ly/AbC#x") == "https://bit.ly/AbC"  # the short link's case is kept, the fragment dropped
    for refused_url in ("https://user@bit.ly/AbC", "https://user:secret@bit.ly/AbC", "ftp://bit.ly/AbC"):
        with pytest.raises(ValueError):
            canonical_url(refused_url)


def test_every_public_row_loads_and_a_widening_domain_is_refused():
    seeds = public_seeds()
    rows = [public_list_row_from_seed(seed, 0, seed["expiresAt"]) for seed in seeds]
    for seed, row in zip(seeds, rows):
        parsed = parse_list_row(row)
        assert parsed.source is ListSource.PUBLIC_FEED and parsed.source_id == seed["sourceId"]
    assert len({(row["PK"], row["SK"]) for row in rows}) == len(rows)
    listed_domain = public_seed("1password-login.com")
    assert public_list_row_from_seed(listed_domain, 0, "2027-04-25T00:00:00Z")["ttlEpochSeconds"] == 1808611200
    with pytest.raises(ValueError):
        public_list_row_from_seed({**listed_domain, "value": "app-ledger.co.com"}, 0, "2027-04-25T00:00:00Z")  # would list co.com
    with pytest.raises(ValueError):
        public_list_row_from_seed({**listed_domain, "source": "sample"}, 0, "2027-04-25T00:00:00Z")


class FailingListStore:
    def get_row(self, kind, key):
        raise ListLookupFailed("table unreadable")


def test_check_reads_stale_and_failed_reads_as_unavailable_never_clean(capsys):
    indicators = [Indicator(IndicatorKind.DOMAIN, "parcel-fee.example")]
    domain_source = SimpleNamespace(indicator_kinds=(IndicatorKind.DOMAIN,), last_sync_at="2026-10-27T09:00:00Z", sync_interval_minutes=60)
    fresh = check(SeedListStore([]), indicators, [domain_source], "2026-10-27T12:00:00Z")
    assert (fresh.hits, fresh.coverage, fresh.cache_until) == ((), ScamListCoverageState.COMPLETE, "2026-10-28T12:00:00Z")
    stale = check(SeedListStore([]), indicators, [domain_source], "2026-10-27T12:00:01Z")
    assert (stale.hits, stale.coverage, stale.cache_until) == ((), ScamListCoverageState.UNAVAILABLE, "2026-10-27T12:05:01Z")
    never_synced = SimpleNamespace(indicator_kinds=(IndicatorKind.DOMAIN,), last_sync_at=None, sync_interval_minutes=60)
    assert check(SeedListStore([]), indicators, [never_synced], "2026-10-27T12:00:00Z").coverage is ScamListCoverageState.UNAVAILABLE

    capsys.readouterr()
    failed = check(FailingListStore(), indicators, [domain_source], "2026-10-27T12:00:00Z")
    assert (failed.hits, failed.coverage, failed.cache_until) == ((), ScamListCoverageState.UNAVAILABLE, "2026-10-27T12:05:00Z")
    metric_lines = [line for line in capsys.readouterr().out.splitlines() if LIST_CHECK_FAILED_METRIC_NAME in line]
    assert len(metric_lines) == 1 and json.loads(metric_lines[0])[LIST_CHECK_FAILED_METRIC_NAME] == 1
    assert failed.as_payload() == {"reasons": [], "attributions": [], "coverage": "unavailable", "cacheUntil": "2026-10-27T12:05:00Z"}


def test_a_listed_sender_from_a_feed_is_warn_never_hold(aws):  # noqa: F811
    listed_domain = public_seed("1password-login.com")
    graph.get_table().put_item(Item=public_list_row_from_seed(listed_domain, 1, "2099-01-01T00:00:00Z"))
    sender_address = "billing@1password-login.com"
    reasons = lookup_sender_reasons(DynamoListStore(), sender_address, "1password-login.com", "Billing", CONTACTS)
    assert reasons == [ReasonCode.LISTED_SENDER_DOMAIN]
    for intent in Intent:
        for confidence in (0.1, 0.95):
            verdict = EmailVerdict(claimed_person=None, intent=intent, confidence=confidence, summary="A note.")
            outcome = decide(verdict, None, None, sender_address, reasons)
            assert outcome.decision is not PolicyDecision.HOLD
            if intent is Intent.NONE:
                assert (outcome.decision, outcome.warn_reason) == (PolicyDecision.WARN, WarnReason.LISTED_SENDER)

    graph.get_table().put_item(Item=public_list_row_from_seed(listed_domain, 1, "2026-01-01T00:00:00Z"))  # expired, TTL not yet run
    assert lookup_sender_reasons(DynamoListStore(), sender_address, "1password-login.com", "Billing", CONTACTS) == []


def test_check_returns_link_listed_for_a_listed_domain():
    newsletter = parse_eml((SAMPLES_PATH / "newsletter-link.eml").read_bytes())
    assessments = assess_links(newsletter.links, frozenset())
    indicators = list_indicators_of(assessments, newsletter.sender_domain)
    assert indicators[0].kind is IndicatorKind.DOMAIN  # the sender's domain is asked first
    assert indicators[1:] == [Indicator(IndicatorKind.DOMAIN, "zelle-pay-help.example"),
                              Indicator(IndicatorKind.URL, "https://zelle-pay-help.example/renew")]
    listed_row = public_list_row_from_seed({
        "kind": "domain", "value": "zelle-pay-help.example", "category": "scam", "strength": "listed", "tier": 1,
        "reason": "On the AMF blacklist (Usurpation) since 2026-10-08", "attribution": AMF_ATTRIBUTION, "addedAt": NOW,
        "source": "public-feed", "sourceId": AMF,
    }, 1, "2027-04-25T15:00:00Z")
    marked = mark_listed_links(assessments, check(SeedListStore([listed_row]), indicators, [], NOW))
    assert marked[0].reasons == (ReasonCode.LINK_TEXT_HREF_MISMATCH, ReasonCode.LINK_LISTED)
    assert marked[0].as_row()["listedBy"] == AMF_ATTRIBUTION
    quiet_verdict = EmailVerdict(claimed_person=None, intent=Intent.NONE, confidence=0.1, summary="A newsletter.")
    outcome = decide(quiet_verdict, None, None, newsletter.sender_address, link_reasons(marked, newsletter))
    assert (outcome.decision, outcome.warn_reason) == (PolicyDecision.WARN, WarnReason.LINK_REASONS)

    mailto_href = "mailto:hello@gardenweekly.example"
    mailto_link = EmailLink("Write to us", mailto_href, "", False, link_kind_of_href(mailto_href), HiddenBy.NONE,
                            LinkSource.HTML_PART, 0.1, False)
    only_sender = list_indicators_of(assess_links((mailto_link,), frozenset()), newsletter.sender_domain)
    assert [indicator.kind for indicator in only_sender] == [IndicatorKind.DOMAIN]  # the mailto link adds no indicator


def test_a_listed_url_marks_its_link_with_the_asked_indicator():
    """A URL row's SK is a hash: the hit must carry the link's own URL indicator, or mark_listed_links never matches it."""
    newsletter = parse_eml((SAMPLES_PATH / "newsletter-link.eml").read_bytes())
    assessments = assess_links(newsletter.links, frozenset())
    indicators = list_indicators_of(assessments, newsletter.sender_domain)
    listed_url_row = public_list_row_from_seed({
        "kind": "url", "value": "HTTPS://Zelle-Pay-Help.example/renew", "category": "scam", "strength": "listed", "tier": 1,
        "reason": "Reported to KISA as a phishing site, 2024-12-31", "attribution": "KISA 피싱사이트 URL", "addedAt": NOW,
        "source": "public-feed", "sourceId": KISA_2024,
    }, 1, "2027-04-25T15:00:00Z")
    result = check(SeedListStore([listed_url_row]), indicators, [], NOW)
    assert [hit.indicator for hit in result.hits] == [Indicator(IndicatorKind.URL, "https://zelle-pay-help.example/renew")]
    marked = mark_listed_links(assessments, result)
    assert marked[0].reasons == (ReasonCode.LINK_TEXT_HREF_MISMATCH, ReasonCode.LINK_LISTED)
    assert marked[0].as_row()["listedBy"] == "KISA 피싱사이트 URL"


def test_scam_list_coverage_unavailable_when_any_live_source_is_stale(aws, capsys):  # noqa: F811
    sender_only = [Indicator(IndicatorKind.DOMAIN, "gardenweekly.example")]
    put_source({**seed_source(AMF), "lastSyncAt": NOW, "lastGeneration": 1})
    put_source({**seed_source(KISA_2024), "lastSyncAt": "2026-10-01T00:00:00Z", "lastGeneration": 1})  # over 3 weekly intervals
    assert check_against_live_sources(sender_only, NOW).coverage is ScamListCoverageState.UNAVAILABLE

    put_source({**seed_source(KISA_2024), "lastSyncAt": NOW, "lastGeneration": 1})
    put_source({**seed_source(PHISHING_DATABASE), "lastSyncAt": "2026-01-01T00:00:00Z", "lastGeneration": 0})  # a stale snapshot
    links_and_sender = [*sender_only, Indicator(IndicatorKind.URL, "https://zelle-pay-help.example/renew")]
    assert check_against_live_sources(links_and_sender, NOW).coverage is ScamListCoverageState.COMPLETE

    pre_sx3_item = source_row_from_seed({**seed_source(AMF), "lastSyncAt": NOW, "lastGeneration": 1})
    del pre_sx3_item["feedParser"]
    graph.get_table().put_item(Item=pre_sx3_item)
    capsys.readouterr()
    assert check_against_live_sources(sender_only, NOW).coverage is ScamListCoverageState.UNAVAILABLE
    metric_lines = [line for line in capsys.readouterr().out.splitlines() if LIST_CHECK_FAILED_METRIC_NAME in line]
    assert len(metric_lines) == 1
