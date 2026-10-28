"""pidge-list-dispatch on moto: live-fetch feeds (AMF, KISA) sync through the production handler with a fake opener (no
network) and a snapshot feed through sync_snapshot_sources; check() returns their rows with attribution; a failed fetch
never stamps and reads unavailable once stale; a 304 keeps the generation; oversize, non-https and shrunken feeds and
refused licences write nothing; the bucket diff rewrites only changed rows; and the list keeper proposes a SOURCE# row
(never live) behind its flag, with the real Strands loop and a scripted model."""

import email.message
import io
import json
import urllib.error
import urllib.request
import urllib.response
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from boto3.dynamodb.conditions import Key

from pidge_list_dispatch import feed_fetch, handler
from shared import feed_parsers, graph, list_sync
from shared.budget import BudgetLedger, BudgetLine, KeeperSessionOutcome, KeeperSessionResult
from shared.enums import IndicatorKind, ListStrength, ProposedBy, ReaderModelChoice
from shared.list_enums import FeedFetchOutcome, ScamListCoverageState
from shared.list_keeper import ListKeeperSession, SourceProposal, build_list_keeper_agent, run_list_keeper_session, run_prompt
from shared.lists import Indicator, list_row_from_seed
from shared.sources import (
    LicenceRefused, LicenceTag, SourceState, approve_source, check_against_live_sources, list_newest_live_sources,
    list_source_versions, parse_source_row, source_row_from_seed,
)
from tests.helpers.scripted_model import ScriptedModel, tool_call_step, verdict_step
from tests.test_sources import AMF, PHISHING_DATABASE, put_source, seed_source
from tests.test_verdict import aws  # noqa: F401

NOW = "2026-10-27T15:00:00Z"
ONE_DAY_LATER = "2026-10-28T15:00:00Z"
THREE_DAILY_INTERVALS_LATER = "2026-10-30T15:00:00Z"
JUST_PAST_THREE_DAILY_INTERVALS = "2026-10-30T15:00:01Z"
NINETY_ONE_DAYS_LATER = "2027-01-26T15:00:00Z"
KISA_2024 = "kr-kisa-phishing-2024"
TEST_FEED = "test-feed"
FEED_URL = "https://feed.example/amf.csv"
FEEDS_FIXTURE_DIRECTORY = Path(__file__).resolve().parent / "fixtures" / "feeds"
AMF_FEED = (FEEDS_FIXTURE_DIRECTORY / "amf_5rows.csv").read_bytes()
KISA_FEED = (FEEDS_FIXTURE_DIRECTORY / "kisa_5rows.csv").read_bytes()
OVER_THE_CAP = 52428801
AMF_SYNCED = {"sourceId": AMF, "outcome": "fetched", "generation": 1, "written": 4, "deleted": 0, "skipped": 1}
PUBLIC_SNAPSHOT = json.loads(list_sync.PUBLIC_SNAPSHOT_FILE.read_text(), parse_float=Decimal)
SAMPLE_LISTS_PATH = list_sync.PUBLIC_SNAPSHOT_FILE.parent / "lists.sample.json"
PROPOSAL = {
    "has_source": True, "name": "Phishing.Database active domains", "indicator_kind": "domain",
    "mapping": {"format": "text", "encoding": "utf-8"}, "ttl_days": 180, "sync_interval_minutes": 1440,
    "attribution_text": "Phishing.Database, MIT License", "attribution_url": "https://github.com/Phishing-Database/Phishing.Database",
}
LICENCE_STEP = tool_call_step("check_licence", {"terms_url": "https://github.com/Phishing-Database/Phishing.Database/blob/master/LICENSE"})


class FakeFeedResponse:
    """What an opener returns: the body read in chunks, the given headers, and a count of read() calls and bytes read."""

    def __init__(self, body: bytes, headers: tuple[tuple[str, str], ...] = ()):
        self.body, self.bytes_read, self.read_call_count = body, 0, 0
        self.headers = email.message.Message()
        for header_name, header_value in headers:
            self.headers[header_name] = header_value

    def read(self, amount: int) -> bytes:
        self.read_call_count += 1
        chunk = self.body[self.bytes_read:self.bytes_read + amount]
        self.bytes_read += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exception_details):
        return False


class FakeFeedOpener:
    """Stands in for build_feed_opener(): each open() records the request and gives the next scripted answer."""

    def __init__(self, answers: list):
        self.answers, self.requests = list(answers), []

    def open(self, request, timeout):
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class RedirectingHttpsHandler(urllib.request.BaseHandler):
    """Answers every https request with a 302 to the target, ahead of the real https handler of a production opener."""

    handler_order = 100

    def __init__(self, target_url: str):
        self.target_url = target_url

    def https_open(self, request):
        headers = email.message.Message()
        headers["Location"] = self.target_url
        response = urllib.response.addinfourl(io.BytesIO(b""), headers, request.full_url, code=302)
        response.msg = "Found"
        return response


def http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(FEED_URL, status, "scripted", email.message.Message(), None)


def install_opener(monkeypatch, answers: list) -> FakeFeedOpener:
    opener = FakeFeedOpener(answers)
    monkeypatch.setattr(feed_fetch, "build_feed_opener", lambda: opener)
    return opener


def handle_sync_at(monkeypatch, now: str, trigger: str = "sync_now") -> list[dict]:
    monkeypatch.setattr(graph, "now_timestamp", lambda: now)
    return handler.handle({"trigger": trigger}, None)["synced"]


def list_rows(kind: IndicatorKind) -> dict[str, dict]:
    return {item["SK"]: item for item in graph.get_table().query(KeyConditionExpression=Key("PK").eq(f"LIST#{kind}"))["Items"]}


def feed_list_rows() -> dict[str, dict]:
    return {f"{kind}|{key}": row for kind in IndicatorKind for key, row in list_rows(kind).items()}


def source_stamp(source_id: str) -> tuple[str | None, int | None]:
    source = list_source_versions(source_id)[0]
    return source.last_sync_at, source.last_generation


def metric_records(printed_lines: list[str], metric_name: str) -> list[dict]:
    return [json.loads(line) for line in printed_lines if metric_name in line]


@pytest.fixture
def feeds_mock(aws, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LIST_FEEDS_MOCK", "true")


def build_test_feed_seed(source_id: str = TEST_FEED) -> dict:
    """A live, licence-checked snapshot source shaped like the Phishing.Database seed, hourly, rows kept 30 days."""
    return {**seed_source(PHISHING_DATABASE), "id": source_id, "syncIntervalMinutes": 60, "ttlDays": 30,
            "lastSyncAt": None, "lastGeneration": None}


def parcel_fee_rows(indexes) -> list[dict]:
    return [{"kind": "domain", "value": f"parcel-fee-{index:02d}.example", "category": "scam", "strength": "listed", "tier": 1,
             "reason": "Sample parcel-fee lure domain", "attribution": "Test feed", "addedAt": NOW, "expiresAt": NOW,
             "source": "public-feed", "sourceId": TEST_FEED} for index in indexes]


def snapshot_attribution(source_id: str, value: str) -> str:
    return next(row["attribution"] for row in PUBLIC_SNAPSHOT if row["sourceId"] == source_id and row["value"] == value)


def parcel_fee_row(index: int) -> dict | None:
    return graph.get_table().get_item(Key={"PK": "LIST#domain", "SK": f"parcel-fee-{index:02d}.example"}).get("Item")


def test_a_fetched_feed_and_a_snapshot_seed_rows_that_check_returns_with_attribution(feeds_mock, monkeypatch):
    monkeypatch.setenv("LIVE_FEEDS_ENABLED", "true")
    put_source(seed_source(PHISHING_DATABASE))
    put_source(seed_source(AMF))
    install_opener(monkeypatch, [FakeFeedResponse(AMF_FEED)])
    assert handle_sync_at(monkeypatch, NOW) == [AMF_SYNCED]
    reports = list_sync.sync_snapshot_sources(NOW)
    assert [(report.source_id, report.written_count, report.deleted_count, report.generation) for report in reports] == [
        (PHISHING_DATABASE, 142, 0, 1)]
    assert {(source.source_id, source.last_sync_at, source.last_generation) for source in list_newest_live_sources()} == {
        (AMF, NOW, 1), (PHISHING_DATABASE, NOW, 1)}
    synced = graph.get_table().get_item(Key={"PK": "LIST#domain", "SK": "1password-login.com"})["Item"]
    assert (synced["expiresAt"], synced["ttlEpochSeconds"], synced["generation"]) == ("2027-04-25T15:00:00Z", 1808665200, 1)

    indicators = [Indicator(IndicatorKind.DOMAIN, "Mail.1Password-Login.com"), Indicator(IndicatorKind.DOMAIN, "WWW.Armen.ltd")]
    result = check_against_live_sources(indicators, ONE_DAY_LATER)
    assert [(hit.source_id, hit.strength, hit.attribution) for hit in result.hits] == [
        (PHISHING_DATABASE, ListStrength.LISTED, snapshot_attribution(PHISHING_DATABASE, "1password-login.com")),
        (AMF, ListStrength.LISTED, seed_source(AMF)["attribution"]["text"])]
    assert (result.coverage, result.cache_until) == (ScamListCoverageState.COMPLETE, "2026-10-29T15:00:00Z")
    assert [attribution["sourceId"] for attribution in result.as_payload()["attributions"]] == [PHISHING_DATABASE, AMF]

    stale = check_against_live_sources(indicators, JUST_PAST_THREE_DAILY_INTERVALS)
    assert (len(stale.hits), stale.coverage) == (2, ScamListCoverageState.UNAVAILABLE)
    assert handle_sync_at(monkeypatch, "2026-10-27T16:00:00Z", "sync") == []  # not due, so the opener is never asked


def test_with_live_feeds_off_a_never_synced_feed_leaves_the_sample_rows_answering_complete(aws, monkeypatch):  # noqa: F811
    monkeypatch.delenv("LIVE_FEEDS_ENABLED", raising=False)
    put_source(seed_source(AMF))  # live, with a feedParser, never synced
    sample_seed = next(seed for seed in json.loads(SAMPLE_LISTS_PATH.read_text()) if seed["value"] == "kakaobank-secure-pay.example")
    graph.get_table().put_item(Item=list_row_from_seed(sample_seed))
    indicators = [Indicator(IndicatorKind.DOMAIN, "kakaobank-secure-pay.example"), Indicator(IndicatorKind.DOMAIN, "example.org")]
    result = check_against_live_sources(indicators, NOW)
    assert ([hit.strength for hit in result.hits], result.coverage) == ([ListStrength.LISTED], ScamListCoverageState.COMPLETE)
    monkeypatch.setenv("LIVE_FEEDS_ENABLED", "true")
    assert check_against_live_sources(indicators, NOW).coverage is ScamListCoverageState.UNAVAILABLE


def test_http_failure_marks_the_source_unavailable_never_clean(aws, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setenv("LIVE_FEEDS_ENABLED", "true")
    put_source(seed_source(AMF))
    install_opener(monkeypatch, [FakeFeedResponse(AMF_FEED), http_error(503), http_error(503)])
    assert handle_sync_at(monkeypatch, NOW) == [AMF_SYNCED]
    rows_after_the_sync = feed_list_rows()
    capsys.readouterr()
    assert handle_sync_at(monkeypatch, ONE_DAY_LATER) == [
        {"sourceId": AMF, "outcome": "http_error", "generation": None, "written": 0, "deleted": 0, "skipped": 0}]
    printed = capsys.readouterr().out.splitlines()
    assert [record["outcome"] for record in metric_records(printed, list_sync.FEED_SYNC_FAILED_METRIC_NAME)] == ["http_error"]
    assert metric_records(printed, list_sync.FEED_STALE_METRIC_NAME) == []
    assert source_stamp(AMF) == (NOW, 1)
    assert feed_list_rows() == rows_after_the_sync and "domain|armen.ltd" in rows_after_the_sync

    armen = [Indicator(IndicatorKind.DOMAIN, "armen.ltd")]
    assert check_against_live_sources(armen, THREE_DAILY_INTERVALS_LATER).coverage is ScamListCoverageState.COMPLETE
    stale = check_against_live_sources(armen, JUST_PAST_THREE_DAILY_INTERVALS)
    assert (stale.coverage, [hit.indicator for hit in stale.hits]) == (ScamListCoverageState.UNAVAILABLE, armen)
    capsys.readouterr()
    handle_sync_at(monkeypatch, JUST_PAST_THREE_DAILY_INTERVALS)
    assert len(metric_records(capsys.readouterr().out.splitlines(), list_sync.FEED_STALE_METRIC_NAME)) == 1


def test_not_modified_keeps_the_generation_and_stamps_last_sync(aws, monkeypatch):  # noqa: F811
    put_source(seed_source(AMF))
    opener = install_opener(monkeypatch, [FakeFeedResponse(AMF_FEED, (("ETag", '"amf-v1"'),)), http_error(304),
                                          FakeFeedResponse(AMF_FEED)])
    assert handle_sync_at(monkeypatch, NOW) == [AMF_SYNCED]
    rows_after_the_sync = feed_list_rows()
    assert handle_sync_at(monkeypatch, ONE_DAY_LATER) == [
        {"sourceId": AMF, "outcome": "not_modified", "generation": 1, "written": 0, "deleted": 0, "skipped": 0}]
    assert opener.requests[1].get_header("If-none-match") == '"amf-v1"'
    assert not opener.requests[1].has_header("Accept-encoding")
    assert source_stamp(AMF) == (ONE_DAY_LATER, 1)
    assert feed_list_rows() == rows_after_the_sync

    # Every bucket is now past ttlDays / 2, so no validator is sent and the rows are rewritten with a fresh expiry.
    assert handle_sync_at(monkeypatch, NINETY_ONE_DAYS_LATER) == [{**AMF_SYNCED, "generation": 2}]
    assert (opener.requests[2].get_header("If-none-match"), opener.requests[2].get_header("If-modified-since")) == (None, None)
    assert list_rows(IndicatorKind.DOMAIN)["armen.ltd"]["expiresAt"] == "2027-07-25T15:00:00Z"
    unasked = feed_fetch.fetch_feed(FEED_URL, None, FakeFeedOpener([http_error(304)]))
    assert unasked.outcome is FeedFetchOutcome.HTTP_ERROR  # a 304 to a request that sent no validators


def test_oversize_feed_is_refused_before_parse(aws, monkeypatch):  # noqa: F811
    downgrading_opener = feed_fetch.build_feed_opener()
    downgrading_opener.add_handler(RedirectingHttpsHandler("http://feed.example/amf.csv"))
    assert feed_fetch.fetch_feed(FEED_URL, None, downgrading_opener).outcome is FeedFetchOutcome.HTTP_ERROR
    assert feed_fetch.fetch_feed("http://feed.example/amf.csv", None, FakeFeedOpener([])).outcome is FeedFetchOutcome.HTTP_ERROR

    declared = FakeFeedResponse(b"", (("Content-Length", str(OVER_THE_CAP)),))
    assert feed_fetch.fetch_feed(FEED_URL, None, FakeFeedOpener([declared])).outcome is FeedFetchOutcome.TOO_LARGE
    assert declared.read_call_count == 0
    oversize_body = bytes(OVER_THE_CAP)
    streamed = FakeFeedResponse(oversize_body)
    assert feed_fetch.fetch_feed(FEED_URL, None, FakeFeedOpener([streamed])).outcome is FeedFetchOutcome.TOO_LARGE
    assert 0 < streamed.bytes_read <= OVER_THE_CAP

    def refuse_to_parse(*_):
        raise AssertionError("parse_feed ran on an oversize feed")

    monkeypatch.setattr(feed_parsers, "parse_feed", refuse_to_parse)
    put_source(seed_source(AMF))
    install_opener(monkeypatch, [FakeFeedResponse(oversize_body)])
    assert [report["outcome"] for report in handle_sync_at(monkeypatch, NOW)] == ["too_large"]
    assert source_stamp(AMF) == (None, None) and feed_list_rows() == {}


def test_amf_fixture_writes_3_domain_rows_1_address_row_0_name_fields(aws, monkeypatch):  # noqa: F811
    put_source(seed_source(AMF))
    install_opener(monkeypatch, [FakeFeedResponse(AMF_FEED)])
    assert handle_sync_at(monkeypatch, NOW) == [AMF_SYNCED]
    assert {key: row["strength"] for key, row in list_rows(IndicatorKind.DOMAIN).items()} == {
        "armen.ltd": "listed", "paris-majeste.com": "listed", "couponeau.business": "suspect"}
    assert {key: row["strength"] for key, row in list_rows(IndicatorKind.ADDRESS).items()} == {"contact@velaren-invest.example": "suspect"}
    assert list_rows(IndicatorKind.URL) == {}
    armen = list_rows(IndicatorKind.DOMAIN)["armen.ltd"]
    assert (armen["reason"], armen["attribution"], armen["source"], armen["sourceId"]) == (
        "On the AMF blacklist (Usurpation) since 2026-10-08", seed_source(AMF)["attribution"]["text"], "public-feed", AMF)
    every_item = graph.get_table().scan()["Items"]
    assert "Velaren Invest SAS" not in json.dumps(every_item, default=str, ensure_ascii=False)


def test_kisa_fixture_writes_the_golden_url_hash(aws, monkeypatch):  # noqa: F811
    put_source(seed_source(KISA_2024))
    install_opener(monkeypatch, [FakeFeedResponse(KISA_FEED)])
    assert handle_sync_at(monkeypatch, NOW) == [{**AMF_SYNCED, "sourceId": KISA_2024}]
    assert set(list_rows(IndicatorKind.URL)) == {
        "fee4ca17f2ad7ed02c95cc23988dd1c669dbb663164614e4a3a0f1d064b39267",  # https://link24.kr/7QLHjRG
        "6edb0ef1ae193dd472f0cddf08a9c18da42624b93cd1c29077051f0ccb7ab84f",  # http://yn.k1mn.yachts
        "ddd3c4941bea40d0b8a91b2a7ad009649ee6775d45c14a82f0d7d9fe37d669c7",  # https://productlg.com/job
    }
    assert set(list_rows(IndicatorKind.DOMAIN)) == {"telegram-cod.com"}  # never k1mn.yachts or productlg.com
    reasons = {row["reason"] for row in feed_list_rows().values()}
    assert reasons == {"Reported to KISA as a phishing site, 2024-12-31"}


def test_no_derivatives_and_share_alike_sources_never_sync(aws):  # noqa: F811
    amf_source = parse_source_row(source_row_from_seed(seed_source(AMF)))
    fetched_urls = []

    def recording_fetcher(url, validators):
        fetched_urls.append(url)
        return list_sync.FeedFetchResult(FeedFetchOutcome.FETCHED, AMF_FEED, None)

    for refused_tag in (LicenceTag.COMMERCIAL_OK_NO_DERIVATIVES, LicenceTag.SHARE_ALIKE):
        report = list_sync.sync_fetched_source(replace(amf_source, licence_tag=refused_tag), NOW, recording_fetcher)
        assert (report.outcome, report.generation) == (FeedFetchOutcome.REFUSED_LICENCE, None)
        with pytest.raises(LicenceRefused):
            source_row_from_seed({**seed_source(AMF), "licenceTag": str(refused_tag)})
    assert fetched_urls == [] and feed_list_rows() == {}

    put_source({**seed_source(AMF), "status": "proposed", "licenceTag": str(LicenceTag.SHARE_ALIKE)})
    with pytest.raises(LicenceRefused):
        approve_source(AMF, 1)
    assert list_source_versions(AMF)[0].status is SourceState.PROPOSED


def test_stale_after_three_intervals_reads_unavailable(aws, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LIVE_FEEDS_ENABLED", "true")
    zelle = [Indicator(IndicatorKind.DOMAIN, "zelle-pay-help.example")]
    put_source({**seed_source(PHISHING_DATABASE), "lastSyncAt": "2026-01-01T00:00:00Z", "lastGeneration": 0})
    assert check_against_live_sources(zelle, NOW).coverage is ScamListCoverageState.COMPLETE  # a stale snapshot alone
    put_source(seed_source(AMF))
    assert check_against_live_sources(zelle, NOW).coverage is ScamListCoverageState.UNAVAILABLE  # never synced
    install_opener(monkeypatch, [FakeFeedResponse(AMF_FEED)])
    assert handle_sync_at(monkeypatch, NOW) == [AMF_SYNCED]
    assert check_against_live_sources(zelle, THREE_DAILY_INTERVALS_LATER).coverage is ScamListCoverageState.COMPLETE
    assert check_against_live_sources(zelle, JUST_PAST_THREE_DAILY_INTERVALS).coverage is ScamListCoverageState.UNAVAILABLE


def test_a_feed_that_shrinks_below_half_is_refused_as_a_dry_run(aws, monkeypatch):  # noqa: F811
    put_source(seed_source(AMF))
    header_and_one_row = b"".join(AMF_FEED.splitlines(keepends=True)[:2])
    install_opener(monkeypatch, [FakeFeedResponse(AMF_FEED), FakeFeedResponse(header_and_one_row)])
    assert handle_sync_at(monkeypatch, NOW) == [AMF_SYNCED]
    rows_after_the_sync = feed_list_rows()
    assert handle_sync_at(monkeypatch, ONE_DAY_LATER) == [
        {"sourceId": AMF, "outcome": "refused_dry_run", "generation": None, "written": 0, "deleted": 0, "skipped": 0}]
    assert feed_list_rows() == rows_after_the_sync and len(rows_after_the_sync) == 4
    assert source_stamp(AMF) == (NOW, 1)


def test_the_bucket_diff_writes_only_changed_rows(feeds_mock):
    put_source(build_test_feed_seed())
    source = list_source_versions(TEST_FEED)[0]
    assert len({list_sync.bucket_of(IndicatorKind.DOMAIN, f"parcel-fee-{index:02d}.example") for index in range(1, 14)}) == 13
    first = list_sync.sync_source(source, parcel_fee_rows(range(1, 13)), NOW)
    assert (first.written_count, first.deleted_count, first.generation) == (12, 0, 1)
    second = list_sync.sync_source(source, parcel_fee_rows([*range(1, 12), 13]), NOW)
    assert (second.written_count, second.deleted_count, second.generation) == (1, 1, 2)
    assert [parcel_fee_row(index)["generation"] for index in range(1, 12)] == [1] * 11
    assert parcel_fee_row(13)["generation"] == 2 and parcel_fee_row(12) is None
    assert list_source_versions(TEST_FEED)[0].last_generation == 2


def test_an_empty_snapshot_or_one_without_the_mock_is_refused_and_never_stamped(aws, monkeypatch, capsys):  # noqa: F811
    monkeypatch.delenv("LIST_FEEDS_MOCK", raising=False)
    with pytest.raises(list_sync.SnapshotMockRequired):
        list_sync.sync_snapshot_sources(NOW)

    monkeypatch.setenv("LIST_FEEDS_MOCK", "true")
    put_source({**build_test_feed_seed("test-empty"), "lastSyncAt": "2026-10-20T00:00:00Z"})
    capsys.readouterr()
    assert list_sync.sync_snapshot_sources(NOW) == []
    assert list_source_versions("test-empty")[0].last_sync_at == "2026-10-20T00:00:00Z"
    assert graph.get_table().get_item(Key={"PK": "SOURCE#test-empty", "SK": list_sync.BUCKETS_SORT_KEY}).get("Item") is None
    metric_lines = [line for line in capsys.readouterr().out.splitlines() if list_sync.LIST_SYNC_EMPTY_METRIC_NAME in line]
    assert len(metric_lines) == 1 and json.loads(metric_lines[0])["source"] == "test-empty"


def keeper_with_scripted_model(session: ListKeeperSession, steps: list) -> tuple:
    agent = build_list_keeper_agent(ReaderModelChoice.HAIKU_4_5, session)
    scripted_model = ScriptedModel(SourceProposal, {run_prompt(session): steps})
    agent.model = scripted_model
    return agent, scripted_model


def test_the_list_keeper_proposes_a_source_from_a_mocked_sample_behind_its_flag(feeds_mock, monkeypatch):
    monkeypatch.delenv("LIST_KEEPER_ENABLED", raising=False)
    assert handler.handle({"trigger": "onboard", "sourceId": PHISHING_DATABASE}, None) == {"started": False}
    assert list_source_versions(PHISHING_DATABASE) == []

    ledger = BudgetLedger(BudgetLine.LISTS, "2026-10-27")
    session = ListKeeperSession(PHISHING_DATABASE, ledger)
    steps = [tool_call_step("read_source", {}), tool_call_step("fetch_sample", {}), LICENCE_STEP, verdict_step(PROPOSAL)]
    agent, scripted_model = keeper_with_scripted_model(session, steps)
    assert run_list_keeper_session(agent, session) == KeeperSessionResult(KeeperSessionOutcome.PROPOSED, 1)
    assert scripted_model.raised_errors == [] and len(scripted_model.recorded_calls) == 4
    proposed = list_source_versions(PHISHING_DATABASE)[0]
    assert (proposed.version, proposed.status, proposed.proposed_by, proposed.licence_tag, proposed.feed_parser) == (
        1, SourceState.PROPOSED, ProposedBy.KEEPER, LicenceTag.UNVERIFIED, None)
    assert proposed.mapping == {"format": "text", "encoding": "utf-8"}
    with pytest.raises(LicenceRefused):
        approve_source(PHISHING_DATABASE, 1)
    budget = graph.get_table().get_item(Key={"PK": "BUDGET#2026-10-27", "SK": "LISTS"})["Item"]
    assert budget["spentUsd"] == Decimal("0.24")  # 4 model calls and 2 fetches

    second_session = ListKeeperSession(PHISHING_DATABASE, ledger)
    missing_column = {**PROPOSAL, "mapping": {"format": "csv", "encoding": "utf-8", "delimiter": ",", "indicator_column": "missing"}}
    second_agent, _ = keeper_with_scripted_model(second_session, [LICENCE_STEP, verdict_step(missing_column)])
    assert run_list_keeper_session(second_agent, second_session) == KeeperSessionResult(KeeperSessionOutcome.REFUSED, None)
    assert len(list_source_versions(PHISHING_DATABASE)) == 1
