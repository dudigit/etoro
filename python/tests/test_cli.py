from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from book_fetcher.api.open_library import PaginationOrder
from book_fetcher.cli import CliOptions, build_search_params


def test_cli_options_normalize_pydantic_validated_values(tmp_path: Path) -> None:
    output = tmp_path / "books.json"

    options = CliOptions.model_validate(
        {
            "title": " Python Crash Course ",
            "author_name": " Eric Matthes ",
            "contact_email": "dev@example.com",
            "publish_year": 2015,
            "language": " eng ",
            "subject": " programming ",
            "limit": 10,
            "max_concurrent_requests": 2,
            "pagination_order": "unordered",
            "output": output,
        }
    )

    assert options.title == "Python Crash Course"
    assert options.author_name == "Eric Matthes"
    assert options.publish_year == 2015
    assert options.language == "eng"
    assert options.subject == "programming"
    assert options.contact_email == "dev@example.com"
    assert options.max_concurrent_requests == 2
    assert options.pagination_order is PaginationOrder.UNORDERED
    assert options.output == output


def test_cli_options_reject_invalid_limit(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        CliOptions.model_validate(
            {
                "title": "python",
                "contact_email": "dev@example.com",
                "limit": 0,
                "output": tmp_path / "books.json",
            }
        )


def test_cli_options_reject_missing_contact_email(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        CliOptions.model_validate(
            {
                "title": "python",
                "output": tmp_path / "books.json",
            }
        )


def test_cli_options_reject_invalid_concurrency(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        CliOptions.model_validate(
            {
                "title": "python",
                "contact_email": "dev@example.com",
                "max_concurrent_requests": 0,
                "output": tmp_path / "books.json",
            }
        )


def test_build_search_params_uses_api_side_criteria_only(tmp_path: Path) -> None:
    options = CliOptions.model_validate(
        {
            "title": "Python",
            "author_name": "Eric Matthes",
            "publish_year": 2015,
            "language": "eng",
            "subject": "programming",
            "contact_email": "dev@example.com",
            "limit": 10,
            "output": tmp_path / "books.json",
        }
    )

    search_params = build_search_params(options)

    assert search_params.model_dump(exclude_none=True) == {
        "title": "Python",
        "author_name": "Eric Matthes",
        "publish_year": 2015,
        "language": "eng",
        "subject": "programming",
    }
