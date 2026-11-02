"""Structured, single-source contract for the oferty-pracy integration.

This module does not enable scheduled ingestion or establish source access rights.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from jobspy.http_control import ControlledRequests, RequestPolicy
from jobspy.indeed import Indeed
from jobspy.indeed.constant import job_type_codes
from jobspy.model import Country, JobPost, JobType, ScraperInput, Site

UPSTREAM_REVISION = "68b88d6909a2342ad75fbb945c4204a3a7ed6665"
FORK_REVISION = "oferty-contract-1"


class IndeedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    search_term: str = Field(min_length=1, max_length=500)
    location: str | None = Field(default=None, max_length=200)
    country_indeed: Country = Country.POLAND
    results_wanted: int = Field(default=20, ge=1, le=100)
    hours_old: int | None = Field(default=None, ge=0)
    is_remote: bool = False
    job_type: JobType | None = None
    http: RequestPolicy = Field(default_factory=RequestPolicy)

    @field_validator("job_type")
    @classmethod
    def supported_job_type(cls, value: JobType | None) -> JobType | None:
        if value is not None and value not in job_type_codes:
            raise ValueError("job_type is not supported by Indeed")
        return value


class RunResult(BaseModel):
    schema_version: Literal[1] = 1
    source: Literal["indeed"] = "indeed"
    upstream_revision: str = UPSTREAM_REVISION
    fork_revision: str = FORK_REVISION
    started_at: datetime
    finished_at: datetime
    query: dict[str, object]
    status: Literal["completed", "limited", "partial", "failed"]
    stop_reason: str
    market_complete: Literal[False] = False
    skipped_jobs: int = 0
    request_count: int = 0
    missing_descriptions: int = 0
    warnings: list[str] = Field(default_factory=list)
    jobs: list[dict[str, object]] = Field(default_factory=list)


def structured_job(job: JobPost) -> dict[str, object]:
    """Preserve typed fields rather than the legacy DataFrame serializer."""
    fields = {name: getattr(job, name) for name in JobPost.model_fields}
    fields["site"] = "indeed"
    fields["salary_source"] = job.model_dump()["salary_source"]
    fields["missing_fields"] = [] if job.description else ["description"]
    return cast(
        dict[str, object],
        TypeAdapter(dict[str, object]).dump_python(fields, mode="json"),
    )


def collect_indeed(request: IndeedRequest) -> RunResult:
    """Run one explicit query. Callers must establish permission before live use."""
    started_at = datetime.now(timezone.utc)
    scraper = Indeed(policy=request.http)
    try:
        jobs = scraper.scrape(
            ScraperInput(
                site_name=[Site.INDEED], **request.model_dump(exclude={"http"})
            )
        )
    except Exception:
        # Adapter exceptions are never included in the machine-readable result.
        return RunResult(
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            query=request.model_dump(mode="json"),
            status="failed",
            stop_reason="unexpected_error",
            request_count=(
                scraper.session.request_count
                if hasattr(scraper, "session")
                and isinstance(scraper.session, ControlledRequests)
                else 0
            ),
        )
    finally:
        if hasattr(scraper, "session"):
            try:
                scraper.session.close()
            except Exception:
                scraper.warn("session_close_error")
    missing = sum(not job.description for job in jobs)
    status: Literal["completed", "limited", "partial", "failed"]
    if scraper.stop_reason in ("page_error", "rate_limited", "transport_error"):
        status = "partial" if jobs else "failed"
    elif scraper.skipped_jobs or missing:
        status = "partial"
    elif scraper.stop_reason in (
        "result_limit",
        "no_progress",
        "request_limit",
        "deadline",
    ):
        status = "limited"
    else:
        status = "completed"
    return RunResult(
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
        query=request.model_dump(mode="json"),
        status=status,
        stop_reason=scraper.stop_reason,
        skipped_jobs=scraper.skipped_jobs,
        request_count=scraper.request_count,
        missing_descriptions=missing,
        warnings=list(dict.fromkeys(scraper.warnings)),
        jobs=[structured_job(job) for job in jobs],
    )
