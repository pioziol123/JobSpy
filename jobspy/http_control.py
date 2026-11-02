"""Bounded HTTP for the explicit Indeed integration, with no proxy rotation."""

from __future__ import annotations

import math
import time
from email.utils import parsedate_to_datetime
from typing import Any

import requests
from pydantic import BaseModel, ConfigDict, Field

from jobspy.util import RequestsRotating


class RequestPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    max_requests: int = Field(default=10, ge=1, le=100)
    max_seconds: float = Field(default=90, ge=1, le=300)
    timeout_seconds: float = Field(default=15, gt=0, le=30)
    min_interval_seconds: float = Field(default=2, ge=1, le=60)
    max_attempts: int = Field(default=2, ge=1, le=3)


class RequestStopped(Exception):
    """A safe diagnostic code, never an underlying exception or response body."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        # RFC delay-seconds is a nonnegative integer, not a float.
        delay = float(int(value)) if value.isascii() and value.isdigit() else None
        if delay is None:
            delay = max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        return delay if math.isfinite(delay) else None
    except (ValueError, TypeError, OverflowError):
        return None


class ControlledRequests(RequestsRotating):
    def __init__(self, policy: RequestPolicy) -> None:
        super().__init__()
        self.policy = policy
        self.deadline = time.monotonic() + policy.max_seconds
        self.request_count = 0
        self.last_started: float | None = None

    def _wait(self, seconds: float) -> None:
        if time.monotonic() + seconds >= self.deadline:
            raise RequestStopped("deadline")
        if seconds > 0:
            time.sleep(seconds)

    def request(self, *args: Any, **kwargs: Any) -> requests.Response:  # noqa: ANN401
        for attempt in range(self.policy.max_attempts):
            if self.request_count >= self.policy.max_requests:
                raise RequestStopped("request_limit")
            if self.last_started is not None:
                self._wait(
                    max(
                        0.0,
                        self.last_started
                        + self.policy.min_interval_seconds
                        - time.monotonic(),
                    )
                )
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise RequestStopped("deadline")
            kwargs["timeout"] = min(self.policy.timeout_seconds, remaining)
            kwargs["allow_redirects"] = False
            self.last_started = time.monotonic()
            self.request_count += 1
            try:
                response: requests.Response = super().request(*args, **kwargs)
            except (requests.Timeout, requests.ConnectionError):
                if attempt + 1 == self.policy.max_attempts:
                    raise RequestStopped("transport_error") from None
                if self.request_count >= self.policy.max_requests:
                    raise RequestStopped("request_limit") from None
                self._wait(2.0**attempt)
                continue
            if response.status_code not in (429, 500, 502, 503, 504):
                return response
            if attempt + 1 == self.policy.max_attempts:
                if response.status_code == 429:
                    response.close()
                    raise RequestStopped("rate_limited")
                return response
            delay = retry_after(response.headers.get("Retry-After"))
            response.close()
            if self.request_count >= self.policy.max_requests:
                raise RequestStopped("request_limit")
            self._wait(delay if delay is not None else 2.0**attempt)
        raise AssertionError("validated max_attempts must be positive")
