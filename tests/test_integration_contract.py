"""Run-contract acceptance tests using synthetic GraphQL transport only."""

import copy
import json
from datetime import datetime
from unittest.mock import Mock, patch

import requests
from pydantic import ValidationError
from test_salary_provenance import OfflineTestCase, compensation, pay, raw_job

from jobspy.integration import IndeedRequest, collect_indeed
from jobspy.model import Country

SENTINEL = "PRIVATE_transport_response_token_76abc"


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def job(key="1", description="<p>Build software.</p>"):
    raw = raw_job(compensation(pay({"value": 123.456}), currency="PLN"))
    raw["key"] = key
    raw["description"] = {"html": description}
    raw["attributes"] = [{"key": "CF3CP"}, {"key": "DSQF7"}]
    return raw


def page(jobs, cursor=None):
    response = requests.Response()
    response.status_code = 200
    response.json = Mock(
        return_value={
            "data": {
                "jobSearch": {
                    "pageInfo": {"nextCursor": cursor},
                    "results": [{"job": raw} for raw in jobs],
                }
            }
        }
    )
    return response


class IntegrationContractTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.enterContext(
            patch("jobspy.http_control.time.monotonic", self.clock.monotonic)
        )
        self.enterContext(patch("jobspy.http_control.time.sleep", self.clock.sleep))

    def collect(self, responses, **kwargs):
        # Only this innermost transport patch returns data. Other transport and
        # socket guards inherited from OfflineTestCase remain in force.
        with patch(
            "requests.sessions.Session.request", side_effect=responses
        ) as transport:
            result = collect_indeed(IndeedRequest(search_term=" Python ", **kwargs))
        return result, transport

    def test_empty_success_has_explicit_metadata_and_polish_default(self):
        result, transport = self.collect([page([])])
        self.assertEqual(
            (result.status, result.stop_reason), ("completed", "exhausted")
        )
        self.assertEqual(result.jobs, [])
        self.assertEqual(result.skipped_jobs, 0)
        self.assertEqual(result.missing_descriptions, 0)
        self.assertEqual(result.query["search_term"], "Python")
        self.assertEqual(result.query["country_indeed"], "poland")
        self.assertEqual(
            IndeedRequest(search_term="Python").country_indeed, Country.POLAND
        )
        self.assertFalse(result.market_complete)
        self.assertEqual(result.schema_version, 1)
        self.assertEqual(result.source, "indeed")
        self.assertTrue(result.upstream_revision)
        self.assertTrue(result.fork_revision)
        self.assertIsNotNone(result.started_at.utcoffset())
        self.assertLessEqual(result.started_at, result.finished_at)
        self.assertEqual(transport.call_count, 1)
        for field in ("started_at", "finished_at"):
            datetime.fromisoformat(
                json.loads(result.model_dump_json())[field].replace("Z", "+00:00")
            )

    def test_result_limit_is_limited_and_retains_only_requested_jobs(self):
        result, transport = self.collect(
            [page([job("1"), job("2")], "more")], results_wanted=1
        )
        self.assertEqual(
            (result.status, result.stop_reason), ("limited", "result_limit")
        )
        self.assertEqual([record["id"] for record in result.jobs], ["in-1"])
        self.assertEqual(transport.call_count, 1)

    def test_cursor_without_new_jobs_is_no_progress(self):
        for responses, count in (
            ([page([], "more")], 0),
            ([page([job()], "more"), page([job()], "more")], 1),
        ):
            with self.subTest(count=count):
                result, transport = self.collect(responses)
                self.assertEqual(
                    (result.status, result.stop_reason), ("limited", "no_progress")
                )
                self.assertEqual(len(result.jobs), count)
                self.assertEqual(transport.call_count, len(responses))

    def test_invalid_record_is_counted_between_valid_records(self):
        invalid = job("bad")
        invalid["title"] = {"private": SENTINEL}
        with self.assertLogs("jobspy.indeed", level="WARNING") as captured:
            result, _ = self.collect([page([job("1"), invalid, job("2")])])
        self.assertEqual((result.status, result.stop_reason), ("partial", "exhausted"))
        self.assertEqual(result.skipped_jobs, 1)
        self.assertEqual([record["id"] for record in result.jobs], ["in-1", "in-2"])
        self.assertEqual(result.warnings, ["indeed_invalid_job"])
        self.assertNotIn(SENTINEL, result.model_dump_json())
        self.assertNotIn(SENTINEL, "\n".join(captured.output))

    def test_all_invalid_records_are_partial_not_successful_empty(self):
        invalid = job()
        invalid["title"] = None
        result, _ = self.collect([page([invalid])])
        self.assertEqual((result.status, result.stop_reason), ("partial", "exhausted"))
        self.assertEqual(result.jobs, [])
        self.assertEqual(result.skipped_jobs, 1)

    def test_record_without_key_is_skipped_without_discarding_valid_page(self):
        for key in ("missing", None, "", "  ", 123, []):
            with self.subTest(key=key):
                invalid = job("bad")
                if key == "missing":
                    del invalid["key"]
                else:
                    invalid["key"] = key
                result, _ = self.collect([page([job("1"), invalid, job("2")])])
                self.assertEqual(
                    (result.status, result.stop_reason), ("partial", "exhausted")
                )
                self.assertEqual(result.skipped_jobs, 1)
                self.assertEqual(
                    [record["id"] for record in result.jobs], ["in-1", "in-2"]
                )

    def test_missing_descriptions_are_per_record_and_summarised(self):
        result, _ = self.collect([page([job("1", None), job("2", ""), job("3")])])
        self.assertEqual((result.status, result.stop_reason), ("partial", "exhausted"))
        self.assertEqual(result.missing_descriptions, 2)
        self.assertEqual(result.jobs[0]["missing_fields"], ["description"])
        self.assertEqual(result.jobs[1]["missing_fields"], ["description"])
        self.assertEqual(result.jobs[2]["missing_fields"], [])

    def test_page_failures_preserve_preceding_records_and_redact_details(self):
        error_response = requests.Response()
        error_response.status_code = 200
        error_response.json = Mock(return_value={"errors": [{"message": SENTINEL}]})
        malformed = requests.Response()
        malformed.status_code = 200
        malformed.json = Mock(side_effect=ValueError(SENTINEL))
        blocked = requests.Response()
        blocked.status_code = 403
        blocked._content = SENTINEL.encode()
        for error in (RuntimeError(SENTINEL), error_response, malformed, blocked):
            for previous in ([], [page([job()], "more")]):
                with self.subTest(error=type(error).__name__, previous=bool(previous)):
                    with self.assertLogs("jobspy.indeed", level="ERROR") as captured:
                        result, transport = self.collect([*previous, error])
                    self.assertEqual(result.status, "partial" if previous else "failed")
                    self.assertEqual(result.stop_reason, "page_error")
                    self.assertEqual(len(result.jobs), int(bool(previous)))
                    self.assertEqual(transport.call_count, len(previous) + 1)
                    self.assertNotIn(SENTINEL, result.model_dump_json())
                    self.assertNotIn(SENTINEL, "\n".join(captured.output))

    def test_unexpected_exception_is_redacted_without_network_or_logging(self):
        with patch(
            "jobspy.integration.Indeed.scrape", side_effect=RuntimeError(SENTINEL)
        ):
            with patch("logging.Logger._log") as logs:
                result = collect_indeed(IndeedRequest(search_term="Python"))
        self.assertEqual(
            (result.status, result.stop_reason), ("failed", "unexpected_error")
        )
        self.assertEqual(result.jobs, [])
        self.assertNotIn(SENTINEL, result.model_dump_json())
        logs.assert_not_called()
        for guard in self.network_guards:
            guard.assert_not_called()

    def test_json_preserves_nested_types_salary_and_normalized_values(self):
        raw = job()
        original = copy.deepcopy(raw)
        result, _ = self.collect([page([raw])])
        output = json.loads(result.model_dump_json())
        record = output["jobs"][0]
        self.assertIsInstance(record["location"], dict)
        self.assertEqual(record["location"]["city"], "Warsaw")
        self.assertEqual(record["job_type"], ["fulltime"])
        self.assertEqual(record["missing_fields"], [])
        self.assertEqual(record["date_posted"], "2026-01-01")
        self.assertIsNone(record["company_rating"])
        self.assertEqual(record["site"], "indeed")
        self.assertEqual(record["salary_source"], "direct_data")
        self.assertEqual(record["salary_origin"], "employer")
        self.assertEqual(record["min_amount"], 123.46)
        self.assertEqual(
            record["salary_evidence"]["baseSalary"]["range"]["value"], 123.456
        )
        self.assertNotIn("compensation", record)
        self.assertEqual(raw, original)

    def test_query_contains_only_supported_filters(self):
        result, transport = self.collect(
            [page([])],
            location=" Warsaw ",
            hours_old=24,
            is_remote=True,
            job_type="fulltime",
        )
        self.assertEqual(
            result.query,
            {
                "search_term": "Python",
                "location": "Warsaw",
                "country_indeed": "poland",
                "results_wanted": 20,
                "hours_old": 24,
                "is_remote": True,
                "job_type": "fulltime",
                "http": {
                    "max_requests": 10,
                    "max_seconds": 90,
                    "timeout_seconds": 15,
                    "min_interval_seconds": 2,
                    "max_attempts": 2,
                },
            },
        )
        query = transport.call_args.kwargs["json"]["query"]
        self.assertIn('what: "Python"', query)
        self.assertIn('where: "Warsaw"', query)
        self.assertIn('start: "24h"', query)
        self.assertIn("CF3CP", query)
        self.assertIn("DSQF7", query)

    def test_blank_extra_invalid_and_unsupported_input_rejected_before_network(self):
        for changes in (
            {"search_term": "  "},
            {"search_term": ""},
            {"easy_apply": True},
            {"proxies": SENTINEL},
            {"unknown": True},
            {"results_wanted": 0},
            {"results_wanted": 101},
            {"hours_old": -1},
            {"job_type": "other"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValidationError):
                    collect_indeed(
                        IndeedRequest(**{"search_term": "Python", **changes})
                    )
        for guard in self.network_guards:
            guard.assert_not_called()
