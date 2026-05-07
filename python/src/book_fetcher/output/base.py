from __future__ import annotations

from collections.abc import AsyncIterable
from pathlib import Path
from typing import Protocol

from book_fetcher.models import Book


class BookWriter(Protocol):
    """Output writer interface for future formats such as CSV or Markdown."""

    async def write(self, books: AsyncIterable[Book], destination: Path) -> int:
        """Write books to the destination path and return the number of written records."""
        ...
