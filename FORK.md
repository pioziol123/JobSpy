# JobSpy fork for oferty-pracy

Upstream: https://github.com/speedyapply/JobSpy
Fork: https://github.com/pioziol123/JobSpy
Base: `68b88d6909a2342ad75fbb945c4204a3a7ed6665` (package 1.4.1).

Local extensions are additive. The upstream MIT license and attribution remain in LICENSE. The fork does not include a job database, candidate profile, matching logic, or employer research. Using this library does not establish permission to access any job board.

## Salary provenance (Indeed)

`Compensation` and `JobPost` add:

- `salary_origin`: `employer`, `portal_estimate`, or `unknown` (default).
- `salary_evidence`: the validated Indeed compensation object, or null when empty. It preserves the employer and estimated salaries, original currencies, amounts and units. This is not the original HTTP response.

The employer salary still takes precedence. An unsupported pay interval does not silently fall back to the estimate. Missing amounts have unknown origin; zero is an amount. Other boards have unknown origin until adapted individually.

The legacy `salary_source` describes extraction (`direct_data`/`description`), not employer provenance. Annualisation changes the legacy output amounts but leaves salary evidence in its original units. JSON export preserves evidence as an object. CSV is a legacy flat format, not the structured integration protocol.

## Local verification

```sh
uv sync --python 3.13 --all-extras
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check jobspy tests
.venv/bin/mypy
```

Tests use synthetic data and block networking. Live use is not enabled in the oferty-pracy application. The application specifications and Beads tracker own implementation scope and progress; this file owns the maintained fork API notes.

## Structured Indeed integration

`jobspy.integration.collect_indeed(IndeedRequest(...))` returns a Pydantic `RunResult`; use `model_dump_json()` for the transport representation. This is an additional API; legacy `scrape_jobs()` remains available. `schema_version=1`, `fork_revision=oferty-contract-1`, and `upstream_revision` identify its contract and base, not an uncommitted build's Git hash.

The explicit request accepts search_term (required, nonblank), location, country_indeed (default Poland), results_wanted (1–100), hours_old, is_remote, supported Indeed job_type, and http policy. Unknown parameters and unsupported types are rejected. No candidate profile, proxy configuration or credentials enter this API. Establish access rights before calling it against a real board.

The result includes the query, UTC start/end times, jobs, request_count, skipped_jobs, missing_descriptions, safe warnings, status and stop_reason. `market_complete` is always false. `completed` only means the selected query was exhausted without known record/description loss; `limited` means a result/request/time limit or lack of pagination progress; `partial` means some data is missing or a failure followed retained records; `failed` means a page/transport/unexpected failure with no retained jobs. A skipped record or missing description can make a limited query partial while preserving its original stop_reason.

Structured jobs retain location objects, lists, ISO dates, nulls and salary evidence. They add `site=indeed` and `missing_fields` (currently description). They are normalized JobSpy records, not original HTTP payloads. No result proves that an unobserved posting expired.

HTTP policy defaults: 10 attempts overall, 90 seconds, 15-second per-request timeout, 2 seconds minimum between attempt starts, and 2 attempts per request. Allowed bounds are enforced by RequestPolicy. Timeouts and connection errors, 429 and 500/502/503/504 may retry. Valid Retry-After is respected; otherwise bounded backoff applies. Redirects are disabled and all attempts count. 403 does not retry or rotate proxies. Stop codes include request_limit, deadline, rate_limited and transport_error. Time control is cooperative; the application must later provide a hard process timeout and avoid overlapping runs.

The original `scrape_jobs()` path does not automatically opt into these HTTP controls. Only the explicit new integration does so. Other boards are not adapted to this contract yet.
