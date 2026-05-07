from __future__ import annotations

import pytest
from pydantic_core import ValidationError

from book_fetcher.models import OPEN_LIBRARY_BOOK_FIELDS, OpenLibrarySearchResponse


def test_open_library_response_parses_relevant_fields() -> None:
    response = OpenLibrarySearchResponse.model_validate(
        {
            "numFound": 1,
            "docs": [
                {
                    "key": "/works/OL123W",
                    "title": "Fluent Python",
                    "author_name": ["Luciano Ramalho"],
                    "first_publish_year": 2015,
                    "isbn": ["9781491946008"],
                    "edition_count": 4,
                    "ignored_field": "ignored",
                }
            ],
        }
    )

    assert response.num_found == 1
    assert response.books[0].key == "/works/OL123W"
    assert response.books[0].title == "Fluent Python"
    assert response.books[0].authors == ["Luciano Ramalho"]
    assert response.books[0].first_publish_year == 2015
    assert response.books[0].isbn == ["9781491946008"]
    assert response.books[0].edition_count == 4


def test_book_title_must_not_be_blank() -> None:
    with pytest.raises(ValidationError):
        OpenLibrarySearchResponse.model_validate(
            {"numFound": 1, "docs": [{"title": "   ", "author_name": []}]}
        )


def test_open_library_response_accepts_documented_num_found_name() -> None:
    response = OpenLibrarySearchResponse.model_validate({"num_found": 0, "docs": []})

    assert response.num_found == 0


def test_open_library_book_fields_are_derived_from_book_model() -> None:
    assert OPEN_LIBRARY_BOOK_FIELDS == "key,title,author_name,first_publish_year,isbn,edition_count"
