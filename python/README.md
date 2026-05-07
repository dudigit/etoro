# Book Fetcher

Fetch book data from Open Library, validate it with Pydantic, and write JSON output.

## Usage

```bash
uv run book-fetcher \
  --title "Python Crash Course" \
  --author-name "Eric Matthes" \
  --publish-year 2015 \
  --language eng \
  --subject programming \
  --contact-email "you@example.com" \
  --max-concurrent-requests 3 \
  --pagination-order ordered \
  --log-level INFO \
  --output books.json
```

You can also provide the contact email through `BOOK_FETCHER_CONTACT_EMAIL`. Open Library
asks API clients to identify themselves with a `User-Agent` that includes contact information.

## Production Grade Features

- Type-safe data contracts with Pydantic models for CLI options, search parameters, API requests,
  API responses, and normalized book records.
- API-side search criteria only. Search parameters are sent to Open Library as query parameters, so
  the app does not fetch broad results and then filter them locally.
- Identified Open Library requests with a custom `User-Agent` that includes contact information, as
  requested by the Open Library API guidelines.
- Async HTTP client using one `httpx.AsyncClient` instance as an async context manager, enabling
  connection pooling and TCP/TLS connection reuse.
- Explicit HTTP timeout categories for connect, read, write, and pool timeouts.
- Resilient retries with Tenacity using bounded async retries, exponential backoff, and jitter for
  transient disconnects, rate limits, and 5xx responses.
- Circuit breaker protection that temporarily stops outbound requests after repeated upstream
  failures, allowing the API time to recover.
- Memory-conscious streaming using an async generator. Books are fetched page by page and written to
  JSON one record at a time instead of keeping the full result set in memory.
- Ordered and unordered parallel pagination. Pages can be fetched concurrently with
  `--max-concurrent-requests`; use `--pagination-order ordered` to preserve page-number order or
  `--pagination-order unordered` to yield pages as soon as they complete.
- Bounded concurrency with `asyncio.Semaphore`, preventing the client from sending too many page
  requests at once.
- Bounded buffering. In ordered mode, only a small window of pages is in flight or waiting to be
  yielded, which keeps memory usage predictable.
- Atomic JSON output. Results are first written to a temporary file and then moved into place.
- Structured JSON logs with `structlog`, suitable for Kubernetes stdout log collection. Use
  `--log-level DEBUG` for page scheduling, HTTP attempts, retry, circuit breaker, and write-progress
  details.
- Prometheus metrics instrumentation for HTTP attempts and latency, retries, pages fetched, books
  yielded/written, circuit breaker opens/blocks, and low-cardinality error categories.
- Test coverage for parsing, CLI validation, retries, circuit breaker behavior, ordered parallel
  pagination, and streaming JSON output.

## Code Explain

`OpenLibraryClient` stores `_client` as the actual `httpx.AsyncClient` used to make requests. Keeping
one client on the class allows connection reuse and makes it easy to inject a mock client in tests.

`_owns_client` tracks whether `OpenLibraryClient` created that HTTP client itself. If it did, the
client closes it in `aclose()` to avoid leaking resources. If the caller passed an external
`httpx.AsyncClient`, `OpenLibraryClient` leaves closing to the caller because it does not own that
object.

## Metrics

Metrics are defined in `src/book_fetcher/metrics.py` with `prometheus-client`. The current CLI is a
short-lived batch command, so it records metrics in the process registry while the command runs. If
the fetcher is embedded in a long-running API, worker, or Kubernetes service, expose
`book_fetcher.metrics.metrics_text()` from a `/metrics` endpoint or use the standard
`prometheus_client.start_http_server(...)` helper.

Key application metrics:

- `book_fetcher_open_library_requests_total{status_code}` counts HTTP attempts by status code, with
  `transport_error` for network failures before a response exists.
- `book_fetcher_open_library_request_duration_seconds` tracks upstream request latency.
- `book_fetcher_open_library_retries_total` counts scheduled retry attempts.
- `book_fetcher_open_library_pages_fetched_total` counts successfully parsed result pages.
- `book_fetcher_books_yielded_total{pagination_order}` and `book_fetcher_books_written_total` track
  fetch and output volume.
- `book_fetcher_circuit_breaker_opened_total` and
  `book_fetcher_circuit_breaker_blocked_requests_total` track breaker protection.
- `book_fetcher_errors_total{error_type}` groups application errors without high-cardinality labels.

## CLI Call Chain

The command starts at the `book-fetcher` console script, which points to
`book_fetcher.cli:main`.

Common flow for both pagination modes:

1. `main()` builds the argument parser with `build_parser()`.
2. `argparse` parses CLI flags such as `--title`, `--author-name`, `--limit`,
   `--max-concurrent-requests`, `--pagination-order`, and `--output`.
3. `CliOptions.model_validate(...)` validates and normalizes the parsed values.
4. `configure_logging(...)` configures structured JSON logs.
5. `asyncio.run(run(options))` starts the async runtime.
6. `run(...)` creates one `OpenLibraryClient` with `async with`, so the underlying
   `httpx.AsyncClient` connection pool is opened and later closed safely.
7. `build_search_params(options)` converts validated CLI options into
   `OpenLibrarySearchParams`.
8. `JsonBookWriter().write(...)` receives `client.iter_books(...)` as an async stream and writes
   each yielded `Book` into a temporary JSON file.
9. After all yielded books are written, the temporary file replaces the final output path
   atomically.

Ordered pagination flow when `--pagination-order ordered` is selected:

1. `iter_books(...)` validates the page size and concurrency settings.
2. `_search_page(..., page=1)` fetches the first page to learn `num_found` and plan total pages.
3. Books from page 1 are yielded immediately.
4. `_iter_remaining_pages_ordered(...)` schedules a bounded window of page fetch tasks using
   `asyncio.create_task(...)`.
5. Each page fetch calls `_search_page(...)`, which calls `_get_with_retries(...)`.
6. `_get_with_retries(...)` checks the circuit breaker, applies Tenacity retries, and performs the
   actual `httpx.AsyncClient.get(...)` request.
7. Even if later pages finish first, `_iter_remaining_pages_ordered(...)` waits for the next page
   number before yielding. For example, page 3 is held until page 2 is yielded.
8. `JsonBookWriter.write(...)` receives books in page-number order and writes them one record at a
   time.

Unordered pagination flow when `--pagination-order unordered` is selected:

1. `iter_books(...)` still fetches page 1 first to learn `num_found` and plan total pages.
2. Books from page 1 are yielded immediately.
3. `_iter_remaining_pages_unordered(...)` creates bounded concurrent page fetch tasks.
4. Page requests still pass through `_search_page(...)` and `_get_with_retries(...)`, so retries,
   timeouts, circuit breaker behavior, and structured logs are the same as ordered mode.
5. `asyncio.as_completed(...)` yields each remaining page as soon as its request finishes.
6. `JsonBookWriter.write(...)` writes books in completion order, which may differ from page-number
   order. This mode improves throughput when strict result ordering is not required.

## Adding Search Criteria

To add a new API-side search criterion, add the same field name to both `CliOptions` in
`src/book_fetcher/cli.py` and `OpenLibrarySearchParams` in
`src/book_fetcher/api/open_library.py`.

For example, to add `publisher`:

```python
publisher: str | None = None
```

Then add the CLI argument:

```python
parser.add_argument("--publisher", default=None)
```

The names should match because `build_search_params()` maps CLI options into search params with:

```python
OpenLibrarySearchParams.model_validate(options.model_dump())
```

If Open Library expects a different query parameter name, keep the internal field names matching and
use `serialization_alias` only on `OpenLibrarySearchParams`.

## Adding Output Field To Book

The `Book` model is the single source of truth for output fields. The Open Library `fields` query
parameter is derived from `Book`, including aliases such as `authors` mapping to `author_name`.

To add another field to each saved book record, add it to the `Book` model in
`src/book_fetcher/models.py`:

```python
publisher: list[str] = Field(default_factory=list)
```

If the output field name differs from Open Library's response field name, use a Pydantic alias:

```python
authors: list[str] = Field(default_factory=list, alias="author_name")
```

Update tests that check parsed models or JSON output, especially `tests/test_models.py` and
`tests/test_json_writer.py`.

After the field is added to `Book`, it is automatically requested from Open Library and written to
JSON. Fields not defined on `Book` are ignored because the model uses `extra="ignore"`.

## Development

```bash
uv sync --dev
uv run pytest
uv run ruff check .
uv run pyright
```
