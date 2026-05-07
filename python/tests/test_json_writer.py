from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from book_fetcher.models import Book
from book_fetcher.output.json_writer import JsonBookWriter


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_json_book_writer_writes_serialized_books(tmp_path: Path) -> None:
    destination = tmp_path / "nested" / "books.json"

    async def books() -> AsyncIterator[Book]:
        yield Book(
            key="/works/OL123W",
            title="Fluent Python",
            author_name=["Luciano Ramalho"],
            first_publish_year=2015,
        )

    written_count = await JsonBookWriter().write(books(), destination)

    assert written_count == 1
    assert json.loads(destination.read_text(encoding="utf-8")) == [
        {
            "key": "/works/OL123W",
            "title": "Fluent Python",
            "authors": ["Luciano Ramalho"],
            "first_publish_year": 2015,
            "isbn": [],
            "edition_count": None,
        }
    ]
