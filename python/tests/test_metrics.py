from __future__ import annotations

import httpx
import pytest

from book_fetcher import metrics
from book_fetcher.api.open_library import OpenLibraryClient, OpenLibrarySearchParams


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_open_library_client_exports_prometheus_metrics() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"numFound": 1, "docs": [{"title": "Python Crash Course"}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenLibraryClient(
            contact_email="dev@example.com",
            search_url="https://example.test/search.json",
            http_client=http_client,
        )

        await client.search_books(search_params=OpenLibrarySearchParams(title="python"))

    exposition = metrics.metrics_text().decode("utf-8")

    assert 'book_fetcher_open_library_requests_total{status_code="200"}' in exposition
    assert "book_fetcher_open_library_request_duration_seconds_count" in exposition
    assert 'book_fetcher_books_yielded_total{pagination_order="ordered"}' in exposition
