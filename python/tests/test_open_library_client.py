from __future__ import annotations

import asyncio

import httpx
import pytest
from pydantic import ValidationError

from book_fetcher.api.open_library import (
    BookApiError,
    CircuitBreakerOpenError,
    OpenLibraryClient,
    OpenLibrarySearchParams,
    OpenLibrarySearchRequest,
    PaginationOrder,
)
from book_fetcher.models import OPEN_LIBRARY_BOOK_FIELDS


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_search_books_fetches_and_parses_books() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"].startswith("book-fetcher/")
        assert "dev@example.com" in request.headers["user-agent"]
        assert request.url.params["title"] == "Python Crash Course"
        assert request.url.params["author"] == "Eric Matthes"
        assert request.url.params["publish_year"] == "2015"
        assert request.url.params["language"] == "eng"
        assert request.url.params["subject"] == "programming"
        assert request.url.params["limit"] == "2"
        assert request.url.params["page"] == "1"
        assert request.url.params["fields"] == OPEN_LIBRARY_BOOK_FIELDS
        return httpx.Response(
            200,
            json={
                "numFound": 1,
                "docs": [
                    {
                        "title": "Python Crash Course",
                        "author_name": ["Eric Matthes"],
                        "first_publish_year": 2015,
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenLibraryClient(
            contact_email="dev@example.com",
            search_url="https://example.test/search.json",
            http_client=http_client,
        )

        books = await client.search_books(
            search_params=OpenLibrarySearchParams(
                title=" Python Crash Course ",
                author_name=" Eric Matthes ",
                publish_year=2015,
                language=" eng ",
                subject=" programming ",
            ),
            limit=2,
        )

    assert books[0].title == "Python Crash Course"
    assert books[0].authors == ["Eric Matthes"]


@pytest.mark.anyio
async def test_search_books_wraps_http_errors() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenLibraryClient(
            contact_email="dev@example.com",
            search_url="https://example.test/search.json",
            http_client=http_client,
        )

        with pytest.raises(BookApiError, match="request failed"):
            await client.search_books(search_params=OpenLibrarySearchParams(title="python"))


@pytest.mark.anyio
async def test_search_books_retries_transient_transport_errors() -> None:
    request_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            raise httpx.ConnectError("connection reset by peer", request=request)

        return httpx.Response(
            200,
            json={"numFound": 1, "docs": [{"title": "Python Crash Course"}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenLibraryClient(
            contact_email="dev@example.com",
            search_url="https://example.test/search.json",
            retry_backoff_initial_seconds=0,
            retry_backoff_max_seconds=0,
            http_client=http_client,
        )

        books = await client.search_books(
            search_params=OpenLibrarySearchParams(title="Python Crash Course")
        )

    assert request_count == 2
    assert books[0].title == "Python Crash Course"


@pytest.mark.anyio
async def test_search_books_retries_transient_status_codes() -> None:
    request_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            return httpx.Response(503, request=request)

        return httpx.Response(
            200,
            json={"numFound": 1, "docs": [{"title": "Python Crash Course"}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenLibraryClient(
            contact_email="dev@example.com",
            search_url="https://example.test/search.json",
            retry_backoff_initial_seconds=0,
            retry_backoff_max_seconds=0,
            http_client=http_client,
        )

        books = await client.search_books(
            search_params=OpenLibrarySearchParams(title="Python Crash Course")
        )

    assert request_count == 2
    assert books[0].title == "Python Crash Course"


@pytest.mark.anyio
async def test_search_books_rejects_unsafe_limits() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenLibraryClient(
            contact_email="dev@example.com",
            search_url="https://example.test/search.json",
            http_client=http_client,
        )

        with pytest.raises(ValidationError, match="greater than or equal to 1"):
            await client.search_books(
                search_params=OpenLibrarySearchParams(title="python"), limit=0
            )


def test_search_request_serializes_supported_search_params() -> None:
    search_request = OpenLibrarySearchRequest(
        search_params=OpenLibrarySearchParams(
            title="Python",
            author_name="Eric Matthes",
            publish_year=2015,
            language="eng",
            subject="programming",
        ),
        limit=5,
    )

    assert search_request.to_query_params() == {
        "title": "Python",
        "author": "Eric Matthes",
        "publish_year": 2015,
        "language": "eng",
        "subject": "programming",
        "limit": 5,
        "page": 1,
        "fields": OPEN_LIBRARY_BOOK_FIELDS,
    }


def test_search_params_ignore_cli_only_fields() -> None:
    search_params = OpenLibrarySearchParams.model_validate(
        {
            "title": "Python",
            "author_name": "Eric Matthes",
            "contact_email": "dev@example.com",
            "output": "books.json",
            "limit": 10,
        }
    )

    assert search_params == OpenLibrarySearchParams(
        title="Python",
        author_name="Eric Matthes",
    )


def test_search_params_require_at_least_one_criterion() -> None:
    with pytest.raises(ValidationError, match="at least one Open Library search criterion"):
        OpenLibrarySearchParams()


@pytest.mark.anyio
async def test_iter_books_fetches_pages_and_yields_books_incrementally() -> None:
    requested_pages: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested_pages.append(request.url.params["page"])
        if request.url.params["page"] == "1":
            return httpx.Response(
                200,
                json={
                    "numFound": 3,
                    "docs": [
                        {"title": "Book One"},
                        {"title": "Book Two"},
                    ],
                },
            )

        return httpx.Response(
            200,
            json={
                "numFound": 3,
                "docs": [
                    {"title": "Book Three"},
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenLibraryClient(
            contact_email="dev@example.com",
            search_url="https://example.test/search.json",
            http_client=http_client,
        )

        titles = [
            book.title
            async for book in client.iter_books(
                search_params=OpenLibrarySearchParams(title="python"),
                page_size=2,
            )
        ]

    assert requested_pages == ["1", "2"]
    assert titles == ["Book One", "Book Two", "Book Three"]


@pytest.mark.anyio
async def test_iter_books_preserves_order_when_parallel_pages_finish_out_of_order() -> None:
    page_two_can_finish = asyncio.Event()
    active_requests = 0
    max_active_requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active_requests, max_active_requests

        active_requests += 1
        max_active_requests = max(max_active_requests, active_requests)
        page = request.url.params["page"]
        try:
            if page == "1":
                return httpx.Response(
                    200,
                    json={
                        "numFound": 5,
                        "docs": [
                            {"title": "Book One"},
                            {"title": "Book Two"},
                        ],
                    },
                )

            if page == "2":
                await page_two_can_finish.wait()
                return httpx.Response(
                    200,
                    json={
                        "numFound": 5,
                        "docs": [
                            {"title": "Book Three"},
                            {"title": "Book Four"},
                        ],
                    },
                )

            page_two_can_finish.set()
            return httpx.Response(
                200,
                json={
                    "numFound": 5,
                    "docs": [
                        {"title": "Book Five"},
                    ],
                },
            )
        finally:
            active_requests -= 1

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenLibraryClient(
            contact_email="dev@example.com",
            search_url="https://example.test/search.json",
            http_client=http_client,
        )

        titles = [
            book.title
            async for book in client.iter_books(
                search_params=OpenLibrarySearchParams(title="python"),
                page_size=2,
                max_concurrent_requests=2,
            )
        ]

    assert titles == ["Book One", "Book Two", "Book Three", "Book Four", "Book Five"]
    assert max_active_requests == 2


@pytest.mark.anyio
async def test_iter_books_can_yield_unordered_parallel_pages_by_completion_order() -> None:
    page_two_can_finish = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params["page"]
        if page == "1":
            return httpx.Response(
                200,
                json={
                    "numFound": 5,
                    "docs": [
                        {"title": "Book One"},
                        {"title": "Book Two"},
                    ],
                },
            )

        if page == "2":
            await page_two_can_finish.wait()
            return httpx.Response(
                200,
                json={
                    "numFound": 5,
                    "docs": [
                        {"title": "Book Three"},
                        {"title": "Book Four"},
                    ],
                },
            )

        page_two_can_finish.set()
        return httpx.Response(
            200,
            json={
                "numFound": 5,
                "docs": [
                    {"title": "Book Five"},
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenLibraryClient(
            contact_email="dev@example.com",
            search_url="https://example.test/search.json",
            http_client=http_client,
        )

        titles = [
            book.title
            async for book in client.iter_books(
                search_params=OpenLibrarySearchParams(title="python"),
                page_size=2,
                max_concurrent_requests=2,
                pagination_order=PaginationOrder.UNORDERED,
            )
        ]

    assert titles == ["Book One", "Book Two", "Book Five", "Book Three", "Book Four"]


@pytest.mark.anyio
async def test_circuit_breaker_opens_after_repeated_transient_failures() -> None:
    request_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(503, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenLibraryClient(
            contact_email="dev@example.com",
            search_url="https://example.test/search.json",
            max_retries=0,
            retry_backoff_initial_seconds=0,
            retry_backoff_max_seconds=0,
            circuit_breaker_failure_threshold=2,
            circuit_breaker_recovery_seconds=60,
            http_client=http_client,
        )

        with pytest.raises(BookApiError, match="request failed"):
            await client.search_books(search_params=OpenLibrarySearchParams(title="python"))

        with pytest.raises(BookApiError, match="request failed"):
            await client.search_books(search_params=OpenLibrarySearchParams(title="python"))

        with pytest.raises(CircuitBreakerOpenError, match="circuit breaker is open"):
            await client.search_books(search_params=OpenLibrarySearchParams(title="python"))

    assert request_count == 2
