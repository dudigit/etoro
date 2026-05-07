from __future__ import annotations

import json
import tempfile
import time
from collections.abc import AsyncIterable
from pathlib import Path

from book_fetcher import metrics
from book_fetcher.logging import get_logger
from book_fetcher.models import Book

logger = get_logger(__name__)


class JsonBookWriter:
    """Write book data as stable, human-readable JSON."""

    async def write(self, books: AsyncIterable[Book], destination: Path) -> int:
        """Stream books to a JSON file atomically and return the number written."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        logger.info("json_write_started", destination=str(destination))
        started_at = time.perf_counter()

        try:
            with tempfile.NamedTemporaryFile(
                "w",
                delete=False,
                dir=destination.parent,
                encoding="utf-8",
                suffix=".tmp",
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                written_count = 0
                temporary_file.write("[")

                async for book in books:
                    if written_count == 0:
                        temporary_file.write("\n  ")
                    else:
                        temporary_file.write(",\n  ")

                    json.dump(book.model_dump(mode="json"), temporary_file, ensure_ascii=False)
                    written_count += 1
                    metrics.BOOKS_WRITTEN.inc()
                    if written_count % 100 == 0:
                        logger.debug("json_write_progress", written_count=written_count)

                if written_count > 0:
                    temporary_file.write("\n")

                temporary_file.write("]")
                temporary_file.write("\n")

            temporary_path.replace(destination)
            logger.info(
                "json_write_completed", destination=str(destination), written_count=written_count
            )
            return written_count
        except OSError:
            metrics.ERRORS.labels(error_type="json_write").inc()
            raise
        finally:
            metrics.JSON_WRITE_DURATION.observe(time.perf_counter() - started_at)
