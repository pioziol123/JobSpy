"""Offline HTTP-budget tests with a deterministic clock and transport."""

from email.utils import formatdate
from unittest.mock import Mock, patch

import requests
from pydantic import ValidationError
from test_integration_contract import SENTINEL, FakeClock, job, page
from test_salary_provenance import OfflineTestCase

from jobspy.http_control import ControlledRequests, RequestPolicy, RequestStopped
from jobspy.integration import IndeedRequest, collect_indeed


def response(status=200, retry_after=None):
    value = requests.Response()
    value.status_code = status
    value._content = SENTINEL.encode()
    value.close = Mock()
    if retry_after is not None:
        value.headers["Retry-After"] = retry_after
    return value


class HttpControlTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.enterContext(
            patch("jobspy.http_control.time.monotonic", self.clock.monotonic)
        )
        self.enterContext(patch("jobspy.http_control.time.sleep", self.clock.sleep))
        self.enterContext(
            patch("jobspy.http_control.time.time", return_value=1700000000.0)
        )

    def session(self, **policy):
        session = ControlledRequests(RequestPolicy(**policy))
        self.addCleanup(session.close)
        return session

    def test_policy_rejects_nonfinite_out_of_bounds_and_extra_values(self):
        invalid = {
            "max_requests": (0, 101, float("inf"), 1.5),
            "max_seconds": (0, 301, float("inf"), float("nan")),
            "timeout_seconds": (0, -1, 31, float("inf"), float("nan")),
            "min_interval_seconds": (0, 61, float("inf"), float("nan")),
            "max_attempts": (0, 4, 1.5),
            "proxy": (SENTINEL,),
        }
        for name, values in invalid.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ValidationError):
                        IndeedRequest(search_term="Python", http={name: value})
        for guard in self.network_guards:
            guard.assert_not_called()

    def test_attempt_count_spacing_timeout_and_disabled_redirects(self):
        session = self.session(max_seconds=5, timeout_seconds=4)
        with patch(
            "requests.sessions.Session.request", return_value=response()
        ) as transport:
            session.get("https://example.invalid", timeout=999, allow_redirects=True)
            session.get("https://example.invalid")
        self.assertEqual(session.request_count, 2)
        self.assertEqual(self.clock.sleeps, [2])
        self.assertEqual(
            [call.kwargs["timeout"] for call in transport.call_args_list], [4, 3]
        )
        self.assertTrue(
            all(
                call.kwargs["allow_redirects"] is False
                for call in transport.call_args_list
            )
        )

    def test_request_limit_stops_before_wait_or_transport(self):
        session = self.session(max_requests=1)
        with patch(
            "requests.sessions.Session.request", return_value=response()
        ) as transport:
            session.get("https://example.invalid")
            with self.assertRaises(RequestStopped) as caught:
                session.get("https://example.invalid")
        self.assertEqual(caught.exception.reason, "request_limit")
        self.assertEqual(session.request_count, 1)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_deadline_stops_without_sleeping_beyond_budget(self):
        session = self.session(max_seconds=2)
        with patch(
            "requests.sessions.Session.request", return_value=response()
        ) as transport:
            session.get("https://example.invalid")
            with self.assertRaises(RequestStopped) as caught:
                session.get("https://example.invalid")
        self.assertEqual(caught.exception.reason, "deadline")
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(self.clock.sleeps, [])
        self.clock.now += 3
        with self.assertRaises(RequestStopped):
            session.get("https://example.invalid")

    def test_timeout_and_connection_errors_retry_with_bounded_attempts(self):
        for exception in (requests.Timeout, requests.ConnectionError):
            for successes in (True, False):
                with self.subTest(exception=exception, successes=successes):
                    self.clock.now = 100
                    self.clock.sleeps.clear()
                    session = self.session(max_attempts=3)
                    final = response() if successes else exception(SENTINEL)
                    with patch(
                        "requests.sessions.Session.request",
                        side_effect=[exception(SENTINEL), exception(SENTINEL), final],
                    ) as transport:
                        if successes:
                            self.assertIs(session.get("https://example.invalid"), final)
                        else:
                            with self.assertRaises(RequestStopped) as caught:
                                session.get("https://example.invalid")
                            self.assertEqual(str(caught.exception), "transport_error")
                            self.assertIsNone(caught.exception.__cause__)
                    self.assertEqual(transport.call_count, 3)
                    self.assertEqual(session.request_count, 3)
                    self.assertEqual(sum(self.clock.sleeps), 4)

    def test_retry_statuses_honor_seconds_date_and_backoff_then_close_response(self):
        for status in (429, 500, 502, 503, 504):
            for header, delay in (
                ("5", 5),
                (formatdate(1700000007, usegmt=True), 7),
                (None, 2),
                ("bad", 2),
                ("-1", 2),
                ("1.5", 2),
            ):
                with self.subTest(status=status, header=header):
                    self.clock.now = 100
                    self.clock.sleeps.clear()
                    first = response(status, header)
                    final = response()
                    session = self.session()
                    with patch(
                        "requests.sessions.Session.request", side_effect=[first, final]
                    ) as transport:
                        self.assertIs(session.get("https://example.invalid"), final)
                    self.assertEqual(transport.call_count, 2)
                    self.assertEqual(sum(self.clock.sleeps), delay)
                    self.assertEqual(session.request_count, 2)
                    first.close.assert_called_once()
                    final.close.assert_not_called()

    def test_retry_after_exceeding_deadline_stops_without_retry(self):
        first = response(429, "90")
        session = self.session(max_seconds=5)
        with patch(
            "requests.sessions.Session.request", return_value=first
        ) as transport:
            with self.assertRaises(RequestStopped) as caught:
                session.get("https://example.invalid")
        self.assertEqual(str(caught.exception), "deadline")
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(self.clock.sleeps, [])
        first.close.assert_called_once()

    def test_retries_share_request_limit(self):
        for initial in (requests.Timeout(SENTINEL), response(503)):
            with self.subTest(initial=type(initial).__name__):
                session = self.session(max_requests=1)
                with patch(
                    "requests.sessions.Session.request", side_effect=[initial]
                ) as transport:
                    with self.assertRaises(RequestStopped) as caught:
                        session.get("https://example.invalid")
                self.assertEqual(str(caught.exception), "request_limit")
                self.assertEqual(transport.call_count, 1)

    def test_403_other_errors_and_redirects_are_never_retried(self):
        for status in (301, 302, 400, 401, 403, 404, 501):
            with self.subTest(status=status):
                session = self.session()
                value = response(status)
                with patch(
                    "requests.sessions.Session.request", return_value=value
                ) as transport:
                    self.assertIs(session.get("https://example.invalid"), value)
                self.assertEqual(transport.call_count, 1)
                self.assertFalse(transport.call_args.kwargs["allow_redirects"])

    def test_exhausted_429_is_safe_and_closes_response(self):
        value = response(429)
        session = self.session(max_attempts=1)
        with patch("requests.sessions.Session.request", return_value=value):
            with self.assertRaises(RequestStopped) as caught:
                session.get("https://example.invalid")
        self.assertEqual(str(caught.exception), "rate_limited")
        value.close.assert_called_once()

    def test_public_contract_retains_jobs_and_reports_safe_stop_codes(self):
        cases = (
            ({"max_requests": 1}, [], "request_limit", "limited", 1),
            ({"max_seconds": 1}, [], "deadline", "limited", 1),
            ({"max_attempts": 1}, [response(429)], "rate_limited", "partial", 2),
            (
                {"max_attempts": 1},
                [requests.Timeout(SENTINEL)],
                "transport_error",
                "partial",
                2,
            ),
            ({}, [response(403)], "page_error", "partial", 2),
        )
        for policy, errors, reason, status, count in cases:
            with self.subTest(reason=reason):
                self.clock.now = 100
                self.clock.sleeps.clear()
                with patch(
                    "requests.sessions.Session.request",
                    side_effect=[page([job()], "more"), *errors],
                ) as transport:
                    with patch("requests.sessions.Session.close") as close:
                        with self.assertLogs(
                            "jobspy.indeed", level="ERROR"
                        ) as captured:
                            result = collect_indeed(
                                IndeedRequest(search_term="Python", http=policy)
                            )
                self.assertEqual((result.status, result.stop_reason), (status, reason))
                self.assertEqual(len(result.jobs), 1)
                self.assertEqual(result.request_count, count)
                self.assertEqual(transport.call_count, count)
                close.assert_called_once()
                self.assertNotIn(SENTINEL, result.model_dump_json())
                self.assertNotIn(SENTINEL, "\n".join(captured.output))

    def test_first_request_transport_failures_are_failed_and_session_closed(self):
        for error, reason in (
            (requests.ConnectionError(SENTINEL), "transport_error"),
            (response(429), "rate_limited"),
        ):
            with self.subTest(reason=reason):
                with patch("requests.sessions.Session.request", side_effect=[error]):
                    with patch("requests.sessions.Session.close") as close:
                        result = collect_indeed(
                            IndeedRequest(
                                search_term="Python", http={"max_attempts": 1}
                            )
                        )
                self.assertEqual(
                    (result.status, result.stop_reason), ("failed", reason)
                )
                self.assertEqual(result.request_count, 1)
                self.assertEqual(result.jobs, [])
                close.assert_called_once()

    def test_success_also_closes_session(self):
        with patch("requests.sessions.Session.request", return_value=page([])):
            with patch("requests.sessions.Session.close") as close:
                result = collect_indeed(IndeedRequest(search_term="Python"))
        self.assertEqual(result.status, "completed")
        close.assert_called_once()

    def test_cleanup_exception_never_escapes_or_logs_transport_details(self):
        with patch("requests.sessions.Session.request", return_value=page([])):
            with patch(
                "requests.sessions.Session.close", side_effect=RuntimeError(SENTINEL)
            ):
                with patch("logging.Logger._log") as logs:
                    result = collect_indeed(IndeedRequest(search_term="Python"))
        self.assertNotIn(SENTINEL, result.model_dump_json())
        self.assertNotIn(SENTINEL, str(logs.call_args_list))
