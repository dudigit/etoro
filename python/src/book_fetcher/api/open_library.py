from __future__ import annotations

import asyncio
import math
import time
from collections.abc import AsyncIterator
from enum import StrEnum
from types import TracebackType
from typing import Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
    wait_none,
)

from book_fetcher import __version__, metrics
from book_fetcher.logging import get_logger
from book_fetcher.models import OPEN_LIBRARY_BOOK_FIELDS, Book, OpenLibrarySearchResponse

OPEN_LIBRARY_API_VERSION = "search-v1"
OPEN_LIBRARY_SEARCH_URL = "https://openlibrary.org/search.json"
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_READ_TIMEOUT_SECONDS = 30.0
DEFAULT_WRITE_TIMEOUT_SECONDS = 10.0
DEFAULT_POOL_TIMEOUT_SECONDS = 5.0
DEFAULT_LIMIT = 50
MAX_LIMIT = 100
DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BACKOFF_INITIAL_SECONDS = 1.0
DEFAULT_RETRY_BACKOFF_MAX_SECONDS = 8.0
DEFAULT_CIRCUIT_BREAKER_FAILURE_THRESHOLD = 5
DEFAULT_CIRCUIT_BREAKER_RECOVERY_SECONDS = 300.0
DEFAULT_MAX_CONCURRENT_PAGE_REQUESTS = 3
TRANSIENT_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
logger = get_logger(__name__)


class BookApiError(RuntimeError):
    """Raised when fetching or parsing book data fails."""


class CircuitBreakerOpenError(BookApiError):
    """Raised when requests are blocked because the upstream service is unhealthy."""


class _TransientStatusError(httpx.HTTPStatusError):
    """HTTP status error that should be retried."""


class PaginationOrder(StrEnum):
    """Controls how parallel page results are yielded."""

    ORDERED = "ordered"
    UNORDERED = "unordered"


class OpenLibrarySearchParams(BaseModel):
    """Typed search criteria for Open Library's Search API."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    title: str | None = None
    author_name: str | None = Field(default=None, serialization_alias="author")
    publish_year: int | None = Field(default=None, ge=0)
    language: str | None = None
    subject: str | None = None

    @field_validator("title", "author_name", "language", "subject")
    @classmethod
    def normalize_text_criteria(cls, value: str | None) -> str | None:
        """Trim text criteria and convert blank values to None."""
        if value is None:
            return None

        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def require_at_least_one_criterion(self) -> OpenLibrarySearchParams:
        """Reject requests that would query Open Library without any search criteria."""
        if (
            self.title is None
            and self.author_name is None
            and self.publish_year is None
            and self.language is None
            and self.subject is None
        ):
            raise ValueError("at least one Open Library search criterion is required")
        return self


class OpenLibrarySearchRequest(BaseModel):
    """Validated request parameters for Open Library's Search API."""

    model_config = ConfigDict(frozen=True)

    search_params: OpenLibrarySearchParams
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    page: int = Field(default=1, ge=1)

    def to_query_params(self) -> dict[str, str | int]:
        """Serialize the request into Open Library query parameters."""
        params = self.search_params.model_dump(
            by_alias=True,
            exclude_none=True,
            mode="json",
        )
        query_params: dict[str, str | int] = {
            key: value for key, value in params.items() if isinstance(value, str | int)
        }
        query_params.update(
            {
                "limit": self.limit,
                "page": self.page,
                "fields": OPEN_LIBRARY_BOOK_FIELDS,
            }
        )
        return query_params


class OpenLibraryClientConfig(BaseModel):
    """Validated client identity and endpoint configuration."""

    model_config = ConfigDict(frozen=True)

    contact_email: str
    app_name: str = "book-fetcher"
    search_url: str = OPEN_LIBRARY_SEARCH_URL

    @field_validator("contact_email")
    @classmethod
    def contact_email_must_be_identifying(cls, value: str) -> str:
        """Ensure Open Library requests include a usable contact email."""
        contact_email = value.strip()
        if "@" not in contact_email or contact_email.startswith("@") or contact_email.endswith("@"):
            raise ValueError("contact_email must be a valid contact email for Open Library")
        return contact_email

    @field_validator("app_name")
    @classmethod
    def app_name_must_not_be_blank(cls, value: str) -> str:
        """Normalize and validate the app name used in the User-Agent header."""
        app_name = value.strip()
        if not app_name:
            raise ValueError("app_name must not be blank")
        return app_name


class OpenLibraryClient:
    """Small typed client for Open Library's search API."""

    def __init__(
        self,
        *,
        contact_email: str,
        app_name: str = "book-fetcher",
        search_url: str = OPEN_LIBRARY_SEARCH_URL,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
        write_timeout_seconds: float = DEFAULT_WRITE_TIMEOUT_SECONDS,
        pool_timeout_seconds: float = DEFAULT_POOL_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_initial_seconds: float = DEFAULT_RETRY_BACKOFF_INITIAL_SECONDS,
        retry_backoff_max_seconds: float = DEFAULT_RETRY_BACKOFF_MAX_SECONDS,
        circuit_breaker_failure_threshold: int = DEFAULT_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
        circuit_breaker_recovery_seconds: float = DEFAULT_CIRCUIT_BREAKER_RECOVERY_SECONDS,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Create an async Open Library client with pooling, retries, and breaker state."""
        config = OpenLibraryClientConfig(
            contact_email=contact_email,
            app_name=app_name,
            search_url=search_url,
        )
        self._search_url = config.search_url
        self._headers = {
            "Accept": "application/json",
            "User-Agent": (
                f"{config.app_name}/{__version__} "
                f"({config.contact_email}; {OPEN_LIBRARY_API_VERSION})"
            ),
        }
        if max_retries < 0:
            raise ValueError("max_retries must be greater than or equal to 0")
        if retry_backoff_initial_seconds < 0:
            raise ValueError("retry_backoff_initial_seconds must be greater than or equal to 0")
        if retry_backoff_max_seconds < 0:
            raise ValueError("retry_backoff_max_seconds must be greater than or equal to 0")
        if circuit_breaker_failure_threshold < 1:
            raise ValueError("circuit_breaker_failure_threshold must be greater than or equal to 1")
        if circuit_breaker_recovery_seconds < 0:
            raise ValueError("circuit_breaker_recovery_seconds must be greater than or equal to 0")

        self._max_retries = max_retries
        self._retry_backoff_initial_seconds = retry_backoff_initial_seconds
        self._retry_backoff_max_seconds = retry_backoff_max_seconds
        self._circuit_breaker_failure_threshold = circuit_breaker_failure_threshold
        self._circuit_breaker_recovery_seconds = circuit_breaker_recovery_seconds
        self._circuit_breaker_failure_count = 0
        self._circuit_breaker_opened_at: float | None = None
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=connect_timeout_seconds,
                read=read_timeout_seconds,
                write=write_timeout_seconds,
                pool=pool_timeout_seconds,
            ),
            follow_redirects=False,
        )
        logger.info(
            "open_library_client_initialized",
            search_url=self._search_url,
            owns_client=self._owns_client,
            max_retries=self._max_retries,
            circuit_breaker_failure_threshold=self._circuit_breaker_failure_threshold,
        )

    async def __aenter__(self) -> Self:
        """Enter the async context manager without creating additional resources."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close owned HTTP resources when leaving an async context manager."""
        await self.aclose()

    async def aclose(self) -> None:
        """Close the internal HTTP client when this instance created it."""
        if self._owns_client:
            await self._client.aclose()
            logger.debug("open_library_client_closed")

    async def search_books(
        self,
        *,
        search_params: OpenLibrarySearchParams,
        limit: int = DEFAULT_LIMIT,
        max_concurrent_requests: int = DEFAULT_MAX_CONCURRENT_PAGE_REQUESTS,
        pagination_order: PaginationOrder = PaginationOrder.ORDERED,
    ) -> list[Book]:
        """Collect up to `limit` books into a list.

        This is a convenience method used by tests and small call sites. The CLI uses
        `iter_books()` directly so results can stream to the output writer without loading the full
        result set into memory.
        """
        OpenLibrarySearchRequest(search_params=search_params, limit=limit)
        logger.info("search_books_started", limit=limit)
        return [
            book
            async for book in self.iter_books(
                search_params=search_params,
                page_size=limit,
                max_books=limit,
                max_concurrent_requests=max_concurrent_requests,
                pagination_order=pagination_order,
            )
        ]

    async def iter_books(
        self,
        *,
        search_params: OpenLibrarySearchParams,
        page_size: int = DEFAULT_LIMIT,
        max_books: int | None = None,
        max_concurrent_requests: int = DEFAULT_MAX_CONCURRENT_PAGE_REQUESTS,
        pagination_order: PaginationOrder = PaginationOrder.ORDERED,
    ) -> AsyncIterator[Book]:
        """Yield books page by page using ordered or unordered parallel pagination."""
        OpenLibrarySearchRequest(search_params=search_params, limit=page_size)
        if max_books is not None and max_books < 1:
            raise ValueError("max_books must be greater than or equal to 1")
        if max_concurrent_requests < 1:
            raise ValueError("max_concurrent_requests must be greater than or equal to 1")

        logger.info(
            "book_iteration_started",
            page_size=page_size,
            max_books=max_books,
            max_concurrent_requests=max_concurrent_requests,
            pagination_order=pagination_order.value,
        )
        yielded_count = 0
        first_page = await self._search_page(
            search_params=search_params,
            page=1,
            limit=page_size,
        )
        if not first_page.books:
            logger.info(
                "book_iteration_completed", yielded_count=0, total_found=first_page.num_found
            )
            return

        for book in first_page.books:
            metrics.OPEN_LIBRARY_BOOKS_YIELDED.labels(pagination_order=pagination_order.value).inc()
            yield book
            yielded_count += 1
            if max_books is not None and yielded_count >= max_books:
                return

        if len(first_page.books) < page_size or yielded_count >= first_page.num_found:
            return

        total_pages = math.ceil(first_page.num_found / page_size)
        if max_books is not None:
            total_pages = min(total_pages, math.ceil(max_books / page_size))
        logger.info(
            "parallel_pagination_planned",
            total_found=first_page.num_found,
            total_pages=total_pages,
            page_size=page_size,
            max_concurrent_requests=max_concurrent_requests,
            pagination_order=pagination_order.value,
        )

        if pagination_order is PaginationOrder.UNORDERED:
            async for book in self._iter_remaining_pages_unordered(
                search_params=search_params,
                page_size=page_size,
                max_books=max_books,
                max_concurrent_requests=max_concurrent_requests,
                first_page_total_found=first_page.num_found,
                first_page_yielded_count=yielded_count,
                total_pages=total_pages,
            ):
                metrics.OPEN_LIBRARY_BOOKS_YIELDED.labels(
                    pagination_order=pagination_order.value
                ).inc()
                yield book
            return

        async for book in self._iter_remaining_pages_ordered(
            search_params=search_params,
            page_size=page_size,
            max_books=max_books,
            max_concurrent_requests=max_concurrent_requests,
            first_page_total_found=first_page.num_found,
            first_page_yielded_count=yielded_count,
            total_pages=total_pages,
        ):
            metrics.OPEN_LIBRARY_BOOKS_YIELDED.labels(pagination_order=pagination_order.value).inc()
            yield book

    async def _iter_remaining_pages_ordered(
        self,
        *,
        search_params: OpenLibrarySearchParams,
        page_size: int,
        max_books: int | None,
        max_concurrent_requests: int,
        first_page_total_found: int,
        first_page_yielded_count: int,
        total_pages: int,
    ) -> AsyncIterator[Book]:
        """Fetch remaining pages in parallel but yield them in page-number order."""
        yielded_count = first_page_yielded_count

        semaphore = asyncio.Semaphore(max_concurrent_requests)
        pending_pages: dict[int, asyncio.Task[OpenLibrarySearchResponse]] = {}
        next_page_to_schedule = 2
        next_page_to_yield = 2

        async def fetch_page(page: int) -> OpenLibrarySearchResponse:
            """Fetch one page while respecting the configured concurrency limit."""
            async with semaphore:
                return await self._search_page(
                    search_params=search_params,
                    page=page,
                    limit=page_size,
                )

        try:
            while next_page_to_yield <= total_pages:
                while (
                    next_page_to_schedule <= total_pages
                    and len(pending_pages) < max_concurrent_requests
                ):
                    logger.debug("page_fetch_scheduled", page=next_page_to_schedule)
                    pending_pages[next_page_to_schedule] = asyncio.create_task(
                        fetch_page(next_page_to_schedule)
                    )
                    next_page_to_schedule += 1

                search_response = await pending_pages.pop(next_page_to_yield)
                if not search_response.books:
                    break

                for book in search_response.books:
                    yield book
                    yielded_count += 1
                    if max_books is not None and yielded_count >= max_books:
                        return

                if (
                    len(search_response.books) < page_size
                    or yielded_count >= first_page_total_found
                ):
                    break

                next_page_to_yield += 1
        finally:
            for task in pending_pages.values():
                task.cancel()
            logger.info("book_iteration_completed", yielded_count=yielded_count)

    async def _iter_remaining_pages_unordered(
        self,
        *,
        search_params: OpenLibrarySearchParams,
        page_size: int,
        max_books: int | None,
        max_concurrent_requests: int,
        first_page_total_found: int,
        first_page_yielded_count: int,
        total_pages: int,
    ) -> AsyncIterator[Book]:
        """Fetch remaining pages in parallel and yield each page as soon as it completes."""
        yielded_count = first_page_yielded_count
        semaphore = asyncio.Semaphore(max_concurrent_requests)

        async def fetch_page(page: int) -> tuple[int, OpenLibrarySearchResponse]:
            """Fetch one page and return its page number with the response."""
            async with semaphore:
                return (
                    page,
                    await self._search_page(
                        search_params=search_params,
                        page=page,
                        limit=page_size,
                    ),
                )

        pending_pages = {
            asyncio.create_task(fetch_page(page)) for page in range(2, total_pages + 1)
        }
        try:
            for completed_page in asyncio.as_completed(pending_pages):
                page, search_response = await completed_page
                logger.debug("unordered_page_yield_started", page=page)
                if not search_response.books:
                    continue

                for book in search_response.books:
                    yield book
                    yielded_count += 1
                    if max_books is not None and yielded_count >= max_books:
                        return

                if yielded_count >= first_page_total_found:
                    return
        finally:
            for task in pending_pages:
                task.cancel()
            logger.info("book_iteration_completed", yielded_count=yielded_count)

    async def _search_page(
        self,
        *,
        search_params: OpenLibrarySearchParams,
        page: int,
        limit: int,
    ) -> OpenLibrarySearchResponse:
        """Fetch and validate one Open Library search result page."""
        search_request = OpenLibrarySearchRequest(
            search_params=search_params,
            limit=limit,
            page=page,
        )
        logger.debug("open_library_page_fetch_started", page=page, limit=limit)

        try:
            response = await self._get_with_retries(search_request)
            payload = response.json()
        except httpx.HTTPError as exc:
            metrics.ERRORS.labels(error_type="http_error").inc()
            logger.error("open_library_request_failed", page=page, error=str(exc), exc_info=True)
            raise BookApiError(f"Open Library request failed: {exc}") from exc
        except ValueError as exc:
            metrics.ERRORS.labels(error_type="invalid_json").inc()
            logger.error("open_library_invalid_json", page=page, error=str(exc), exc_info=True)
            raise BookApiError("Open Library returned invalid JSON") from exc

        try:
            search_response = OpenLibrarySearchResponse.model_validate(payload)
        except ValidationError as exc:
            metrics.ERRORS.labels(error_type="schema_validation").inc()
            logger.error("open_library_schema_validation_failed", page=page, exc_info=True)
            raise BookApiError("Open Library response did not match the expected schema") from exc

        metrics.OPEN_LIBRARY_PAGES_FETCHED.inc()
        logger.info(
            "open_library_page_fetch_completed",
            page=page,
            books_count=len(search_response.books),
            total_found=search_response.num_found,
        )
        return search_response

    async def _get_with_retries(self, search_request: OpenLibrarySearchRequest) -> httpx.Response:
        """Execute an HTTP request with circuit-breaker checks and Tenacity retries."""
        self._ensure_circuit_closed()

        try:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception_type((httpx.TransportError, _TransientStatusError)),
                stop=stop_after_attempt(self._max_retries + 1),
                wait=self._retry_wait_strategy(),
                before_sleep=self._log_before_retry,
                reraise=True,
            ):
                with attempt:
                    attempt_number = attempt.retry_state.attempt_number
                    logger.debug(
                        "open_library_http_attempt_started",
                        attempt=attempt_number,
                        page=search_request.page,
                    )
                    started_at = time.perf_counter()
                    metrics.OPEN_LIBRARY_INFLIGHT_REQUESTS.inc()
                    try:
                        response = await self._client.get(
                            self._search_url,
                            params=search_request.to_query_params(),
                            headers=self._headers,
                        )
                    except httpx.TransportError:
                        metrics.OPEN_LIBRARY_REQUESTS.labels(status_code="transport_error").inc()
                        raise
                    finally:
                        metrics.OPEN_LIBRARY_INFLIGHT_REQUESTS.dec()
                        metrics.OPEN_LIBRARY_REQUEST_DURATION.observe(
                            time.perf_counter() - started_at
                        )

                    metrics.OPEN_LIBRARY_REQUESTS.labels(
                        status_code=str(response.status_code)
                    ).inc()
                    if response.status_code in TRANSIENT_STATUS_CODES:
                        logger.warning(
                            "open_library_transient_status",
                            status_code=response.status_code,
                            page=search_request.page,
                            attempt=attempt_number,
                        )
                        raise _TransientStatusError(
                            f"transient Open Library response: {response.status_code}",
                            request=response.request,
                            response=response,
                        )

                    response.raise_for_status()
                    self._record_circuit_success()
                    logger.debug(
                        "open_library_http_attempt_completed",
                        status_code=response.status_code,
                        page=search_request.page,
                        attempt=attempt_number,
                    )
                    return response
        except (httpx.TransportError, _TransientStatusError):
            self._record_circuit_failure()
            raise

        raise BookApiError("Open Library request failed after retry attempts")

    def _log_before_retry(self, retry_state: RetryCallState) -> None:
        """Emit structured details before Tenacity sleeps for the next retry."""
        next_sleep = retry_state.next_action.sleep if retry_state.next_action is not None else None
        metrics.OPEN_LIBRARY_RETRIES.inc()
        logger.warning(
            "open_library_retry_scheduled",
            attempt=retry_state.attempt_number,
            next_sleep_seconds=next_sleep,
            error=str(retry_state.outcome.exception()) if retry_state.outcome is not None else None,
        )

    def _retry_wait_strategy(self):
        """Build the Tenacity wait strategy for retry backoff."""
        if self._retry_backoff_initial_seconds == 0 and self._retry_backoff_max_seconds == 0:
            return wait_none()

        return wait_exponential_jitter(
            initial=max(self._retry_backoff_initial_seconds, 0.001),
            max=max(self._retry_backoff_max_seconds, 0.001),
        )

    def _ensure_circuit_closed(self) -> None:
        """Block requests while the circuit breaker is open."""
        if self._circuit_breaker_opened_at is None:
            return

        elapsed_seconds = time.monotonic() - self._circuit_breaker_opened_at
        if elapsed_seconds < self._circuit_breaker_recovery_seconds:
            metrics.CIRCUIT_BREAKER_BLOCKED_REQUESTS.inc()
            logger.warning(
                "circuit_breaker_open_request_blocked",
                elapsed_seconds=elapsed_seconds,
                recovery_seconds=self._circuit_breaker_recovery_seconds,
            )
            raise CircuitBreakerOpenError(
                "Open Library circuit breaker is open; upstream is temporarily unhealthy"
            )

        self._circuit_breaker_opened_at = None
        self._circuit_breaker_failure_count = 0
        logger.info("circuit_breaker_half_open_after_recovery")

    def _record_circuit_success(self) -> None:
        """Reset breaker failure state after a successful upstream request."""
        self._circuit_breaker_failure_count = 0
        self._circuit_breaker_opened_at = None
        logger.debug("circuit_breaker_success_recorded")

    def _record_circuit_failure(self) -> None:
        """Track an upstream failure and open the circuit when the threshold is reached."""
        self._circuit_breaker_failure_count += 1
        logger.warning(
            "circuit_breaker_failure_recorded",
            failure_count=self._circuit_breaker_failure_count,
            failure_threshold=self._circuit_breaker_failure_threshold,
        )
        if self._circuit_breaker_failure_count >= self._circuit_breaker_failure_threshold:
            self._circuit_breaker_opened_at = time.monotonic()
            metrics.CIRCUIT_BREAKER_OPENED.inc()
            logger.error(
                "circuit_breaker_opened",
                failure_count=self._circuit_breaker_failure_count,
                recovery_seconds=self._circuit_breaker_recovery_seconds,
            )
