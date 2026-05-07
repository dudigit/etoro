from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from book_fetcher.api.open_library import (
    DEFAULT_LIMIT,
    DEFAULT_MAX_CONCURRENT_PAGE_REQUESTS,
    MAX_LIMIT,
    BookApiError,
    OpenLibraryClient,
    OpenLibrarySearchParams,
    PaginationOrder,
)
from book_fetcher.logging import configure_logging, get_logger
from book_fetcher.output.json_writer import JsonBookWriter

logger = get_logger(__name__)


class CliOptions(BaseModel):
    """Validated command options after parsing raw CLI arguments."""

    model_config = ConfigDict(frozen=True)

    title: str | None = None
    author_name: str | None = None
    publish_year: int | None = Field(default=None, ge=0)
    language: str | None = None
    subject: str | None = None
    contact_email: str
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    max_concurrent_requests: int = Field(default=DEFAULT_MAX_CONCURRENT_PAGE_REQUESTS, ge=1)
    pagination_order: PaginationOrder = PaginationOrder.ORDERED
    log_level: str = "INFO"
    output: Path

    @field_validator("title", "author_name", "language", "subject")
    @classmethod
    def normalize_text_criteria(cls, value: str | None) -> str | None:
        """Trim text search values and treat blanks as missing criteria."""
        if value is None:
            return None

        normalized = value.strip()
        return normalized or None

    @field_validator("contact_email")
    @classmethod
    def contact_email_must_be_identifying(cls, value: str) -> str:
        """Validate the contact email required for identified Open Library requests."""
        contact_email = value.strip()
        if "@" not in contact_email or contact_email.startswith("@") or contact_email.endswith("@"):
            raise ValueError("contact_email must be a valid contact email for Open Library")
        return contact_email

    @field_validator("output")
    @classmethod
    def expand_output_path(cls, value: Path) -> Path:
        """Expand user-relative output paths before writing files."""
        return value.expanduser()

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        """Normalize and validate the configured structured-log level."""
        log_level = value.strip().upper()
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if log_level not in valid_levels:
            raise ValueError(f"log_level must be one of {sorted(valid_levels)}")
        return log_level

    @model_validator(mode="after")
    def require_search_criteria(self) -> CliOptions:
        """Ensure CLI options contain at least one Open Library search criterion."""
        OpenLibrarySearchParams.model_validate(self.model_dump())
        return self


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser for the book-fetcher command."""
    parser = argparse.ArgumentParser(description="Fetch book data from Open Library.")
    parser.add_argument(
        "--title", default=None, help="Book title to search via Open Library Search API."
    )
    parser.add_argument(
        "--author-name",
        default=None,
        help="Optional author name to include in the Open Library search.",
    )
    parser.add_argument(
        "--publish-year",
        type=int,
        default=None,
        help="Publication year to send to Open Library as the publish_year search parameter.",
    )
    parser.add_argument(
        "--language",
        default=None,
        help="Language code to send to Open Library, for example eng.",
    )
    parser.add_argument(
        "--subject",
        default=None,
        help="Subject to send to Open Library, for example fantasy.",
    )
    parser.add_argument(
        "--contact-email",
        default=os.getenv("BOOK_FETCHER_CONTACT_EMAIL"),
        help=(
            "Contact email included in the Open Library User-Agent header. "
            "Can also be set with BOOK_FETCHER_CONTACT_EMAIL."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Books to request per Open Library page, 1-{MAX_LIMIT}.",
    )
    parser.add_argument(
        "--max-concurrent-requests",
        type=int,
        default=DEFAULT_MAX_CONCURRENT_PAGE_REQUESTS,
        help="Maximum number of Open Library pages to fetch concurrently.",
    )
    parser.add_argument(
        "--pagination-order",
        choices=[pagination_order.value for pagination_order in PaginationOrder],
        default=PaginationOrder.ORDERED.value,
        help="Yield pages in page-number order or as soon as each page completes.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Log level for structured logs: DEBUG, INFO, WARNING, ERROR, or CRITICAL.",
    )
    parser.add_argument("--output", type=Path, required=True, help="JSON output file path.")
    return parser


def build_search_params(options: CliOptions) -> OpenLibrarySearchParams:
    """Convert validated CLI options into Open Library search parameters."""
    return OpenLibrarySearchParams.model_validate(options.model_dump())


async def run(options: CliOptions) -> int:
    """Run the async fetch-and-write workflow for validated CLI options."""
    logger.info(
        "book_fetcher_started",
        output=str(options.output),
        page_size=options.limit,
        max_concurrent_requests=options.max_concurrent_requests,
        pagination_order=options.pagination_order.value,
    )
    async with OpenLibraryClient(contact_email=options.contact_email) as client:
        written_count = await JsonBookWriter().write(
            client.iter_books(
                search_params=build_search_params(options),
                page_size=options.limit,
                max_concurrent_requests=options.max_concurrent_requests,
                pagination_order=options.pagination_order,
            ),
            options.output,
        )

    logger.info("book_fetcher_completed", written_count=written_count, output=str(options.output))
    print(f"Wrote {written_count} book(s) to {options.output}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse CLI arguments, configure logging, and return a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        options = CliOptions.model_validate(vars(args))
        configure_logging(log_level=options.log_level)
        return asyncio.run(run(options))
    except (BookApiError, OSError, ValueError, ValidationError) as exc:
        configure_logging()
        logger.error("book_fetcher_failed", error=str(exc), exc_info=True)
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
