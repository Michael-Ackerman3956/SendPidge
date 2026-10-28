"""SOURCE# rows: the registry of scam-list feeds, one row per version (SOURCE#<id> / v<n>), listed through UserIndex.

A live source with a feedParser is fetched by the sync; one without is a hand-loaded snapshot. Code refuses live for a
non-commercial-only, unverified, no-derivatives or share-alike licence when a row is parsed, seeded or approved, and again
in the DynamoDB condition; a sync copies only a SYNCABLE_LICENCES source. A proposed source's mapping{} is dry-run
against a sample before it is saved; the peer's hand-written mapping{} in sources.json is stored as it came and never
interpreted, except mapping.url, the https address a live-fetch source is downloaded from.
"""

import csv
import io
import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from shared import graph, lists
from shared.enums import IndicatorKind, ListSource, ProposedBy
from shared.list_enums import FeedParser
from shared.lists import LIST_CHECK_FAILED_METRIC_NAME, SOURCE_ID_PATTERN, CheckResult, Indicator
from shared.metrics import emit_count_metric

logger = logging.getLogger(__name__)

SOURCE_PARTITION_PREFIX = "SOURCE#"
SOURCE_VERSION_PREFIX = "v"
SOURCE_REGISTRY_INDEX_PARTITION = "registry#SOURCE"
MINIMUM_DRY_RUN_INDICATORS = 10
ALLOWED_SAMPLE_ENCODINGS = frozenset({"utf-8", "utf-8-sig", "cp949", "euc-kr", "latin-1"})
REJECT_REASON_MAX_LENGTH = 300
CONDITIONAL_CHECK_FAILED_CODE = "ConditionalCheckFailedException"
COMMENT_LINE_PREFIX = "#"
SCAM_LIST_HIT_METRIC_NAME = "ScamListHit"
FEED_URL_SCHEME = "https"
MINUTES_PER_DAY = 1440


class SourceState(StrEnum):
    """A source version's status; staleness is computed from lastSyncAt, never stored."""

    LIVE = "live"
    PROPOSED = "proposed"
    REJECTED = "rejected"


class SourceKind(StrEnum):
    FEED = "feed"
    LOOKUP = "lookup"
    INCIDENT = "incident"


class LicenceTag(StrEnum):
    COMMERCIAL_OK = "commercial-ok"
    COMMERCIAL_OK_ATTRIBUTION = "commercial-ok-attribution"        # commercial use, attribution required
    COMMERCIAL_OK_NO_DERIVATIVES = "commercial-ok-no-derivatives"  # unchanged reuse only: normalised rows are a change
    SHARE_ALIKE = "share-alike"
    COMMERCIAL_PAID = "commercial-paid"
    NON_COMMERCIAL_ONLY = "non-commercial-only"
    UNVERIFIED = "unverified"


class MappingFormat(StrEnum):
    """What a proposed mapping{} may declare; dry_run_mapping reads only these."""

    TEXT = "text"  # one indicator per line; blank and '#' lines skipped
    CSV = "csv"    # one indicator per row, in mapping["indicatorColumn"]


REFUSED_LIVE_LICENCES = frozenset({
    LicenceTag.NON_COMMERCIAL_ONLY, LicenceTag.UNVERIFIED, LicenceTag.COMMERCIAL_OK_NO_DERIVATIVES, LicenceTag.SHARE_ALIKE,
})
SYNCABLE_LICENCES = frozenset({LicenceTag.COMMERCIAL_OK, LicenceTag.COMMERCIAL_OK_ATTRIBUTION})


class LicenceRefused(ValueError):
    """A source with a REFUSED_LIVE_LICENCES licence may never be live; one outside SYNCABLE_LICENCES is never synced."""


class MappingRejected(ValueError):
    """A mapping{} that does not read at least MINIMUM_DRY_RUN_INDICATORS indicators from its sample."""


class SourceNotProposed(ValueError):
    """Approve or reject on a version that is not (or no longer) proposed."""


@dataclass(frozen=True)
class SourceRow:
    source_id: str
    version: int
    name: str
    kind: SourceKind
    indicator_kinds: tuple[IndicatorKind, ...]
    licence_tag: LicenceTag
    licence_quote: str
    licence_url: str
    attribution_text: str
    attribution_url: str
    sync_interval_minutes: int
    ttl_days: int
    status: SourceState
    proposed_by: ProposedBy
    reject_reason: str | None
    last_sync_at: str | None
    last_generation: int | None
    mapping: dict
    feed_parser: FeedParser | None  # None: a hand-loaded snapshot, never fetched


def require_source_id(source_id: str) -> str:
    if not SOURCE_ID_PATTERN.fullmatch(source_id):
        raise ValueError(f"bad source id {source_id!r}")
    return source_id


def source_partition(source_id: str) -> str:
    return f"{SOURCE_PARTITION_PREFIX}{require_source_id(source_id)}"


def source_key(source_id: str, version: int) -> dict:
    return {"PK": source_partition(source_id), "SK": f"{SOURCE_VERSION_PREFIX}{int(version)}"}


def registry_index_keys(source_id: str, version: int) -> dict:
    return {graph.USER_INDEX_PARTITION_KEY: SOURCE_REGISTRY_INDEX_PARTITION,
            graph.USER_INDEX_SORT_KEY: f"{require_source_id(source_id)}#v{int(version):04d}"}


def optional_text(value) -> str | None:
    return None if value is None else str(value)


def optional_int(value) -> int | None:
    return None if value is None else int(value)


def optional_feed_parser(value) -> FeedParser | None:
    return None if value is None else FeedParser(value)


def require_live_fetch_invariants(source: SourceRow) -> None:
    """A live source with a feed parser keeps its rows for at least two sync intervals and is fetched over https only."""
    if source.sync_interval_minutes * 2 > source.ttl_days * MINUTES_PER_DAY:
        raise ValueError(f"{source.source_id} v{source.version} syncs less often than half its rows' lifetime")
    feed_url = urlsplit(str(source.mapping["url"]))
    if feed_url.scheme != FEED_URL_SCHEME or not feed_url.hostname:
        raise ValueError(f"{source.source_id} v{source.version} has no https feed url")


def require_syncable_licence(source: SourceRow) -> None:
    if source.licence_tag not in SYNCABLE_LICENCES:
        raise LicenceRefused(f"{source.source_id} v{source.version} has licence {source.licence_tag}, which a sync may not copy")


def parse_source_row(item: dict) -> SourceRow:
    """Raises KeyError / ValueError on a broken envelope (feedParser is required, null for a snapshot), LicenceRefused on a
    live row with a refused licence, and ValueError on a live-fetch row that breaks require_live_fetch_invariants."""
    partition = str(item["PK"])
    if not partition.startswith(SOURCE_PARTITION_PREFIX):
        raise ValueError(f"not a SOURCE# row: {partition}")
    source = SourceRow(
        source_id=require_source_id(partition[len(SOURCE_PARTITION_PREFIX):]),
        version=int(str(item["SK"]).removeprefix(SOURCE_VERSION_PREFIX)),
        name=str(item["name"]), kind=SourceKind(item["kind"]),
        indicator_kinds=tuple(IndicatorKind(kind) for kind in item["indicatorKinds"]),
        licence_tag=LicenceTag(item["licenceTag"]), licence_quote=str(item["licenceQuote"]), licence_url=str(item["licenceUrl"]),
        attribution_text=str(item["attribution"]["text"]), attribution_url=str(item["attribution"]["url"]),
        sync_interval_minutes=int(item["syncIntervalMinutes"]), ttl_days=int(item["ttlDays"]),
        status=SourceState(item["status"]), proposed_by=ProposedBy(item["proposedBy"]),
        reject_reason=optional_text(item["rejectReason"]), last_sync_at=optional_text(item["lastSyncAt"]),
        last_generation=optional_int(item["lastGeneration"]), mapping=dict(item["mapping"]),
        feed_parser=optional_feed_parser(item["feedParser"]),
    )
    if source.status is SourceState.LIVE and source.licence_tag in REFUSED_LIVE_LICENCES:
        raise LicenceRefused(f"{source.source_id} v{source.version} is live with licence {source.licence_tag}")
    if source.status is SourceState.LIVE and source.feed_parser is not None:
        require_live_fetch_invariants(source)
    return source


def source_row_from_seed(seed: dict) -> dict:
    """One infra/seed/sources.json record → SOURCE#<id> / v1 with every other field as it came (checkedAt, rows included)."""
    source_id = require_source_id(str(seed["id"]))
    item = {field: value for field, value in seed.items() if field != "id"}
    item.update(source_key(source_id, 1))
    item.update(registry_index_keys(source_id, 1))
    parse_source_row(item)
    return item


def query_all_pages(**query_arguments) -> list[dict]:
    items: list[dict] = []
    while True:
        response = graph.get_table().query(**query_arguments)
        items.extend(response["Items"])
        if "LastEvaluatedKey" not in response:
            return items
        query_arguments["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def list_newest_live_sources() -> list[SourceRow]:
    """The newest live version of every source, by id; read through UserIndex, never a Scan."""
    items = query_all_pages(IndexName=graph.USER_INDEX_NAME,
                            KeyConditionExpression=Key(graph.USER_INDEX_PARTITION_KEY).eq(SOURCE_REGISTRY_INDEX_PARTITION))
    newest_by_id: dict[str, SourceRow] = {}
    for source in (parse_source_row(item) for item in items):
        current = newest_by_id.get(source.source_id)
        if source.status is SourceState.LIVE and (current is None or source.version > current.version):
            newest_by_id[source.source_id] = source
    return sorted(newest_by_id.values(), key=lambda source: source.source_id)


def list_source_versions(source_id: str) -> list[SourceRow]:
    """Every version of one source, oldest first (the BUCKETS row under the same PK is skipped)."""
    items = query_all_pages(KeyConditionExpression=Key("PK").eq(source_partition(source_id)) & Key("SK").begins_with(SOURCE_VERSION_PREFIX))
    return sorted((parse_source_row(item) for item in items), key=lambda source: source.version)


def write_proposed_source(source_id: str, fields: dict) -> int:
    """SOURCE#<id> / v<newest + 1> as proposed by the keeper; code sets status and the bookkeeping fields after `fields`
    (feedParser null, so a keeper row is never fetched), and the condition stops any overwrite."""
    versions = list_source_versions(source_id)
    version = versions[-1].version + 1 if versions else 1
    item = {
        **fields, **source_key(source_id, version), **registry_index_keys(source_id, version),
        "status": str(SourceState.PROPOSED), "proposedBy": str(ProposedBy.KEEPER), "checkedAt": graph.now_timestamp(),
        "lastSyncAt": None, "lastGeneration": None, "rejectReason": None, "hits90d": 0, "feedParser": None,
    }
    parse_source_row(item)
    graph.get_table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    return version


def update_proposed_source(source_id: str, version: int, update_arguments: dict) -> None:
    """One conditional update on a proposed version; a refused condition is SourceNotProposed."""
    try:
        graph.get_table().update_item(Key=source_key(source_id, version), **update_arguments)
    except ClientError as error:
        if error.response["Error"]["Code"] == CONDITIONAL_CHECK_FAILED_CODE:
            raise SourceNotProposed(f"{source_id} v{version} is not proposed") from error
        raise


def approve_source(source_id: str, version: int) -> None:
    """Proposed → live. A refused licence raises LicenceRefused before any write, and the condition refuses it again."""
    item = graph.get_table().get_item(Key=source_key(source_id, version)).get("Item")
    if item is None:
        raise SourceNotProposed(f"{source_id} v{version} does not exist")
    if parse_source_row(item).licence_tag in REFUSED_LIVE_LICENCES:
        raise LicenceRefused(f"{source_id} v{version} has licence {item['licenceTag']}")
    update_proposed_source(source_id, version, {
        "UpdateExpression": "SET #status = :live",
        "ConditionExpression": "#status = :proposed AND NOT licenceTag IN (:nonCommercial, :unverified, :noDerivatives, :shareAlike)",
        "ExpressionAttributeNames": {"#status": "status"},
        "ExpressionAttributeValues": {":live": str(SourceState.LIVE), ":proposed": str(SourceState.PROPOSED),
                                      ":nonCommercial": str(LicenceTag.NON_COMMERCIAL_ONLY), ":unverified": str(LicenceTag.UNVERIFIED),
                                      ":noDerivatives": str(LicenceTag.COMMERCIAL_OK_NO_DERIVATIVES),
                                      ":shareAlike": str(LicenceTag.SHARE_ALIKE)},
    })


def reject_source(source_id: str, version: int, reason: str) -> None:
    if not 1 <= len(reason) <= REJECT_REASON_MAX_LENGTH:
        raise ValueError(f"a reject reason is 1 to {REJECT_REASON_MAX_LENGTH} characters")
    update_proposed_source(source_id, version, {
        "UpdateExpression": "SET #status = :rejected, rejectReason = :reason",
        "ConditionExpression": "#status = :proposed",
        "ExpressionAttributeNames": {"#status": "status"},
        "ExpressionAttributeValues": {":rejected": str(SourceState.REJECTED), ":reason": reason, ":proposed": str(SourceState.PROPOSED)},
    })


def record_sync(source: SourceRow, generation: int, now: str) -> None:
    """Stamps the sync on the live version; called only after every row and the BUCKETS row are written, or on a 304 to the
    validators the last sync stored (its rows unchanged and young)."""
    graph.get_table().update_item(
        Key=source_key(source.source_id, source.version),
        UpdateExpression="SET lastSyncAt = :now, lastGeneration = :generation",
        ConditionExpression="#status = :live",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":now": now, ":generation": generation, ":live": str(SourceState.LIVE)},
    )


def read_text_values(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith(COMMENT_LINE_PREFIX)]


def read_csv_values(text: str, mapping: dict) -> list[str]:
    if "delimiter" not in mapping or "indicatorColumn" not in mapping:
        raise MappingRejected("a csv mapping needs delimiter and indicatorColumn")
    delimiter, column = mapping["delimiter"], mapping["indicatorColumn"]
    if not isinstance(delimiter, str) or len(delimiter) != 1:
        raise MappingRejected("the delimiter is not one character")
    try:
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        if reader.fieldnames is None or column not in reader.fieldnames:
            raise MappingRejected("the indicator column is not in the sample's header")
        return [str(row[column]).strip() for row in reader if row[column] is not None]
    except csv.Error as error:
        raise MappingRejected("the sample does not parse as csv with this mapping") from error


def decode_sample(mapping: dict, sample_bytes: bytes) -> tuple[MappingFormat, str]:
    try:
        mapping_format, encoding = MappingFormat(mapping["format"]), str(mapping["encoding"])
    except (KeyError, ValueError) as error:
        raise MappingRejected("the mapping needs a known format and an encoding") from error
    if encoding not in ALLOWED_SAMPLE_ENCODINGS:
        raise MappingRejected("the encoding is not an allowed sample encoding")
    try:
        return mapping_format, sample_bytes.decode(encoding)
    except UnicodeDecodeError as error:
        raise MappingRejected("the sample does not decode with this encoding") from error


def dry_run_mapping(mapping: dict, indicator_kind: IndicatorKind, sample_bytes: bytes) -> frozenset[str]:
    """The distinct indicator keys a proposed mapping reads from a sample; values that are not of the kind are skipped.
    Fewer than MINIMUM_DRY_RUN_INDICATORS, or a mapping that does not fit the sample, raises MappingRejected."""
    mapping_format, text = decode_sample(mapping, sample_bytes)
    values = read_text_values(text) if mapping_format is MappingFormat.TEXT else read_csv_values(text, mapping)
    keys = set()
    for value in values:
        try:
            keys.add(lists.indicator_key(indicator_kind, value))
        except ValueError:
            continue  # a header, a comment or a value of another kind: not an indicator of this kind
    if len(keys) < MINIMUM_DRY_RUN_INDICATORS:
        raise MappingRejected(f"{len(keys)} {indicator_kind} indicator(s); at least {MINIMUM_DRY_RUN_INDICATORS} are needed")
    return frozenset(keys)


def check_against_live_sources(indicators: list[Indicator], now: str) -> CheckResult:
    """lists.check over the production table and the live registry. Only live-fetch sources count toward staleness, and only
    with LIVE_FEEDS_ENABLED (off, the hourly fetch is off too, so the seeded rows answer alone; a snapshot is bounded by its
    rows' expiresAt). A registry read or row that fails is logged, counted as ListCheckFailed and answered UNAVAILABLE.
    Each public-feed hit is counted as ScamListHit by source."""
    live_fetch_sources: list[SourceRow] = []
    if os.environ.get("LIVE_FEEDS_ENABLED") == "true":
        try:
            live_fetch_sources = [source for source in list_newest_live_sources() if source.feed_parser is not None]
        except (ClientError, KeyError, ValueError) as error:
            logger.error("source registry read failed: %s", error)
            emit_count_metric(LIST_CHECK_FAILED_METRIC_NAME)
            return lists.unavailable_check_result(now)
    result = lists.check(lists.DynamoListStore(), indicators, live_fetch_sources, now)
    for hit in result.hits:
        if hit.source is ListSource.PUBLIC_FEED:
            emit_count_metric(SCAM_LIST_HIT_METRIC_NAME, "source", hit.source_id)
    return result
