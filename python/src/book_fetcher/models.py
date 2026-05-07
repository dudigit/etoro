from __future__ import annotations

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


def _empty_authors() -> list[str]:
    """Return a new default authors list for each Book instance."""
    return []


def _empty_isbn() -> list[str]:
    """Return a new default ISBN list for each Book instance."""
    return []


def _empty_books() -> list[Book]:
    """Return a new default books list for each search response."""
    return []


class Book(BaseModel):
    """Normalized book data used by the application."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    key: str | None = None
    title: str
    authors: list[str] = Field(default_factory=_empty_authors, alias="author_name")
    first_publish_year: int | None = None
    isbn: list[str] = Field(default_factory=_empty_isbn)
    edition_count: int | None = None

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        """Trim book titles and reject blank titles."""
        title = value.strip()
        if not title:
            raise ValueError("title must not be blank")
        return title


class OpenLibrarySearchResponse(BaseModel):
    """Relevant part of the Open Library search response."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    num_found: int = Field(validation_alias=AliasChoices("numFound", "num_found"))
    books: list[Book] = Field(default_factory=_empty_books, alias="docs")


def open_library_fields_for_model(model: type[BaseModel]) -> str:
    """Return Open Library field names represented by a Pydantic model."""

    fields: list[str] = []
    for field_name, field_info in model.model_fields.items():
        fields.append(field_info.alias or field_name)
    return ",".join(fields)


OPEN_LIBRARY_BOOK_FIELDS = open_library_fields_for_model(Book)
