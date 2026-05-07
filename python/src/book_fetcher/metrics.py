from __future__ import annotations

from prometheus_client import REGISTRY, Counter, Gauge, Histogram, generate_latest

OPEN_LIBRARY_REQUESTS = Counter(
    "book_fetcher_open_library_requests_total",
    "Open Library HTTP requests made by the client.",
    ["status_code"],
)
OPEN_LIBRARY_REQUEST_DURATION = Histogram(
    "book_fetcher_open_library_request_duration_seconds",
    "Open Library HTTP request duration in seconds.",
)
OPEN_LIBRARY_RETRIES = Counter(
    "book_fetcher_open_library_retries_total",
    "Open Library retry attempts scheduled by Tenacity.",
)
OPEN_LIBRARY_PAGES_FETCHED = Counter(
    "book_fetcher_open_library_pages_fetched_total",
    "Open Library pages successfully fetched.",
)
OPEN_LIBRARY_BOOKS_YIELDED = Counter(
    "book_fetcher_books_yielded_total",
    "Books yielded by the Open Library client.",
    ["pagination_order"],
)
OPEN_LIBRARY_INFLIGHT_REQUESTS = Gauge(
    "book_fetcher_open_library_inflight_requests",
    "Open Library requests currently in flight.",
)
CIRCUIT_BREAKER_OPENED = Counter(
    "book_fetcher_circuit_breaker_opened_total",
    "Times the Open Library circuit breaker opened.",
)
CIRCUIT_BREAKER_BLOCKED_REQUESTS = Counter(
    "book_fetcher_circuit_breaker_blocked_requests_total",
    "Requests blocked by the Open Library circuit breaker.",
)
ERRORS = Counter(
    "book_fetcher_errors_total",
    "Application errors grouped by low-cardinality error type.",
    ["error_type"],
)
BOOKS_WRITTEN = Counter(
    "book_fetcher_books_written_total",
    "Books written to output files.",
)
JSON_WRITE_DURATION = Histogram(
    "book_fetcher_json_write_duration_seconds",
    "JSON output write duration in seconds.",
)


def metrics_text() -> bytes:
    """Return the current Prometheus metrics exposition payload."""

    return generate_latest(REGISTRY)
