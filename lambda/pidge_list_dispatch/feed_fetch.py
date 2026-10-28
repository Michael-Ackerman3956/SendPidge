"""Downloads one live feed with urllib only. https on the first URL and on every redirect (at most three); no Accept-Encoding
is set, so nothing is asked to be compressed or decompressed; the size cap is checked on Content-Length before any read
and again on the bytes read, before anything is parsed. A 304 is NOT_MODIFIED only when validators were sent. Nothing here
logs: list_sync logs the source id, outcome and byte count."""

import http.client
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from shared.list_enums import FeedFetchOutcome
from shared.list_sync import FeedFetchResult, FeedValidators

MAXIMUM_FEED_BYTES = 50 * 1024 * 1024
FEED_FETCH_TIMEOUT_SECONDS = 60
MAXIMUM_FEED_REDIRECTS = 3
FEED_READ_CHUNK_BYTES = 1024 * 1024
FEED_URL_SCHEME = "https"
NOT_MODIFIED_STATUS = 304


class HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follows at most MAXIMUM_FEED_REDIRECTS redirects, and only to an https URL; any other target is not followed, so the
    redirect status itself raises HTTPError."""

    max_redirections = MAXIMUM_FEED_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != FEED_URL_SCHEME:
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_feed_opener() -> urllib.request.OpenerDirector:
    """An opener with only the https handler (certificates verified), the https-only redirect handler and the error
    handlers: no http, ftp, file or data handler and no proxy handler."""
    opener = urllib.request.OpenerDirector()
    for handler in (urllib.request.HTTPSHandler(), HttpsOnlyRedirectHandler(), urllib.request.HTTPDefaultErrorHandler(),
                    urllib.request.HTTPErrorProcessor()):
        opener.add_handler(handler)
    return opener


def conditional_headers_of(validators: FeedValidators | None) -> dict[str, str]:
    if validators is None:
        return {}
    headers = {}
    if validators.etag is not None:
        headers["If-None-Match"] = validators.etag
    if validators.last_modified is not None:
        headers["If-Modified-Since"] = validators.last_modified
    return headers


def read_capped_body(response) -> bytes | None:
    """At most MAXIMUM_FEED_BYTES + 1 bytes are read; None when the body is longer than the cap."""
    chunks, byte_count = [], 0
    while byte_count <= MAXIMUM_FEED_BYTES:
        chunk = response.read(min(FEED_READ_CHUNK_BYTES, MAXIMUM_FEED_BYTES + 1 - byte_count))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        byte_count += len(chunk)
    return None


def read_feed_response(response) -> FeedFetchResult:
    declared_length = response.headers.get("Content-Length")
    if declared_length is not None and int(declared_length) > MAXIMUM_FEED_BYTES:
        return FeedFetchResult(FeedFetchOutcome.TOO_LARGE, None, None)
    body = read_capped_body(response)
    if body is None:
        return FeedFetchResult(FeedFetchOutcome.TOO_LARGE, None, None)
    validators = FeedValidators(response.headers.get("ETag"), response.headers.get("Last-Modified"))
    return FeedFetchResult(FeedFetchOutcome.FETCHED, body, validators)


def fetch_feed(url: str, validators: FeedValidators | None, opener: urllib.request.OpenerDirector) -> FeedFetchResult:
    """Never raises for a network, TLS, HTTP or header problem: each is HTTP_ERROR (a 304 without validators too)."""
    request_headers = conditional_headers_of(validators)
    try:
        if urlsplit(url).scheme != FEED_URL_SCHEME:
            return FeedFetchResult(FeedFetchOutcome.HTTP_ERROR, None, None)
        request = urllib.request.Request(url, headers=request_headers)
        with opener.open(request, timeout=FEED_FETCH_TIMEOUT_SECONDS) as response:
            return read_feed_response(response)
    except urllib.error.HTTPError as error:
        error.close()
        is_not_modified = error.code == NOT_MODIFIED_STATUS and bool(request_headers)
        return FeedFetchResult(FeedFetchOutcome.NOT_MODIFIED if is_not_modified else FeedFetchOutcome.HTTP_ERROR, None, None)
    except (OSError, http.client.HTTPException, ValueError):  # URLError, timeouts and TLS errors are OSErrors
        return FeedFetchResult(FeedFetchOutcome.HTTP_ERROR, None, None)
