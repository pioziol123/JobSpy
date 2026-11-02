"""Offline contract tests for Indeed salary provenance and public export."""

import copy
import json
import socket
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import curl_cffi.requests
import requests

from jobspy import SCRAPER_MAPPING, scrape_jobs
from jobspy.indeed import Indeed
from jobspy.indeed.model import IndeedCompensation
from jobspy.indeed.util import get_compensation
from jobspy.model import CompensationInterval, JobPost, ScraperInput, Site
from jobspy.records import save


def pay(amounts=None, unit="MONTH"):
    return {"unitOfWork": unit, "range": amounts if amounts is not None else {}}


def compensation(employer=None, estimate=None, currency=None):
    return {
        "baseSalary": employer,
        "currencyCode": currency,
        "estimated": estimate,
    }


def estimate(amounts=None, unit="MONTH", currency="PLN"):
    return {"baseSalary": pay(amounts, unit), "currencyCode": currency}


def raw_job(salary):
    return {
        "key": "synthetic-1",
        "title": "Python developer",
        "sourceEmployerName": "Synthetic employer",
        "dateOnIndeed": 1767225600000,
        "description": {"html": "<p>Build software.</p>"},
        "employer": None,
        "location": {"countryCode": "PL", "city": "Warsaw"},
        "recruit": {"viewJobUrl": "https://example.invalid/apply"},
        "attributes": [],
        "compensation": salary,
    }


class OfflineTestCase(unittest.TestCase):
    """Deny Python sockets and both HTTP transports, including curl's native path."""

    def setUp(self):
        stack = self.enterContext(ExitStack())
        self.network_guards = []
        for target in (
            "socket.socket.connect",
            "socket.socket.connect_ex",
            "socket.create_connection",
            "socket.getaddrinfo",
            "requests.sessions.Session.request",
            "curl_cffi.requests.Session.request",
            "curl_cffi.requests.AsyncSession.request",
        ):
            guard = stack.enter_context(
                patch(target, side_effect=AssertionError("Network forbidden in tests"))
            )
            self.network_guards.append(guard)


class CompensationTests(OfflineTestCase):
    def extract(self, raw):
        return get_compensation(IndeedCompensation.model_validate(raw))

    def test_employer_range_and_legacy_fields(self):
        result = self.extract(
            compensation(pay({"min": 1234.567, "max": 2345.678}), currency="PLN")
        )
        self.assertEqual(result.salary_origin, "employer")
        self.assertEqual((result.min_amount, result.max_amount), (1234.57, 2345.68))
        self.assertEqual(result.currency, "PLN")
        self.assertEqual(result.interval, CompensationInterval.MONTHLY)
        self.assertEqual(result.salary_evidence["baseSalary"]["range"]["min"], 1234.567)

    def test_portal_estimate(self):
        result = self.extract(compensation(estimate=estimate({"min": 100, "max": 200})))
        self.assertEqual(result.salary_origin, "portal_estimate")
        self.assertEqual((result.min_amount, result.max_amount), (100, 200))
        self.assertEqual(result.currency, "PLN")

    def test_both_sources_select_employer_and_retain_both(self):
        raw = compensation(
            pay({"min": 10, "max": 20}, "HOUR"),
            estimate({"min": 2000, "max": 4000}, currency="EUR"),
            "PLN",
        )
        result = self.extract(raw)
        self.assertEqual(result.salary_origin, "employer")
        self.assertEqual((result.min_amount, result.max_amount), (10, 20))
        self.assertEqual(result.currency, "PLN")
        self.assertEqual(result.salary_evidence["estimated"]["currencyCode"], "EUR")
        self.assertEqual(result.salary_evidence["baseSalary"]["unitOfWork"], "HOUR")

    def test_absent_and_empty_estimate_have_no_evidence(self):
        for raw in (
            compensation(),
            compensation(estimate={"baseSalary": None, "currencyCode": None}),
        ):
            with self.subTest(raw=raw):
                result = self.extract(raw)
                self.assertEqual(result.salary_origin, "unknown")
                self.assertIsNone(result.salary_evidence)
                self.assertIsNone(result.min_amount)
                self.assertIsNone(result.max_amount)

    def test_currency_only_retains_evidence_without_inference(self):
        result = self.extract(compensation(currency="PLN"))
        self.assertEqual(result.salary_origin, "unknown")
        self.assertEqual(result.salary_evidence["currencyCode"], "PLN")

    def test_empty_employer_range_does_not_fall_back_to_estimate(self):
        result = self.extract(compensation(pay(), estimate({"value": 5000}), "PLN"))
        self.assertEqual(result.salary_origin, "unknown")
        self.assertIsNone(result.min_amount)
        self.assertIsNone(result.max_amount)
        self.assertEqual(
            result.salary_evidence["estimated"]["baseSalary"]["range"]["value"], 5000
        )

    def test_single_and_one_sided_amounts(self):
        for amounts, expected in (
            ({"value": 25}, (25, 25)),
            ({"min": 25}, (25, None)),
            ({"max": 25}, (None, 25)),
        ):
            with self.subTest(amounts=amounts):
                result = self.extract(compensation(pay(amounts), currency="PLN"))
                self.assertEqual((result.min_amount, result.max_amount), expected)
                self.assertEqual(result.salary_origin, "employer")

    def test_zero_is_salary_for_both_sources(self):
        for raw, origin in (
            (compensation(pay({"value": 0}), currency="PLN"), "employer"),
            (compensation(estimate=estimate({"value": 0})), "portal_estimate"),
        ):
            with self.subTest(origin=origin):
                result = self.extract(raw)
                self.assertEqual(result.salary_origin, origin)
                self.assertEqual((result.min_amount, result.max_amount), (0, 0))
                row = JobPost(
                    job_url="https://example.invalid",
                    title="Zero",
                    **result.model_dump(),
                ).model_dump()
                self.assertEqual(row["salary_source"], "direct_data")

    def test_unsupported_or_missing_period_never_uses_estimate(self):
        for unit in ("FORTNIGHT", None):
            with self.subTest(unit=unit):
                result = self.extract(
                    compensation(
                        pay({"value": 100}, unit), estimate({"value": 200}), "PLN"
                    )
                )
                self.assertEqual(result.salary_origin, "unknown")
                self.assertIsNone(result.interval)
                self.assertIsNone(result.min_amount)
                self.assertIsNone(result.max_amount)
                self.assertEqual(
                    result.salary_evidence["baseSalary"]["unitOfWork"], unit
                )
                self.assertEqual(
                    result.salary_evidence["estimated"]["baseSalary"]["range"]["value"],
                    200,
                )

    def test_extraction_does_not_mutate_input_or_alias_evidence(self):
        raw = compensation(pay({"value": 100}), estimate({"value": 200}), "PLN")
        before = copy.deepcopy(raw)
        model = IndeedCompensation.model_validate(raw)
        model_before = model.model_dump()
        result = get_compensation(model)
        self.assertEqual(raw, before)
        self.assertEqual(model.model_dump(), model_before)
        result.salary_evidence["baseSalary"]["range"]["value"] = 999
        self.assertEqual(model.model_dump(), model_before)
        self.assertEqual(raw, before)


class PipelineTests(OfflineTestCase):
    def process(self, raw):
        scraper = Indeed()
        scraper.scraper_input = ScraperInput(country_indeed="poland")
        scraper.base_url = "https://pl.indeed.com"
        return scraper._process_job(raw)

    def test_actual_adapter_preserves_provenance_and_original_job_fields(self):
        raw = raw_job(compensation(estimate=estimate({"value": 123.456}, "HOUR")))
        before = copy.deepcopy(raw)
        job = self.process(raw)
        self.assertEqual(job.salary_origin, "portal_estimate")
        self.assertEqual(job.min_amount, 123.46)
        self.assertEqual(
            job.salary_evidence["estimated"]["baseSalary"]["range"]["value"], 123.456
        )
        self.assertEqual(job.id, "in-synthetic-1")
        self.assertEqual(job.company, "Synthetic employer")
        self.assertEqual(job.model_dump()["salary_source"], "direct_data")
        self.assertEqual(raw, before)

    def test_public_scrape_annualisation_and_json_export(self):
        salary = compensation(
            pay({"min": 1000, "max": 2000}), estimate({"value": 1500}), "PLN"
        )
        job = self.process(raw_job(salary))
        original = copy.deepcopy(job.model_dump())
        for annualise, interval, minimum in (
            (False, "monthly", 1000),
            (True, "yearly", 12000),
        ):
            with self.subTest(annualise=annualise):
                with patch.object(Indeed, "scrape", return_value=[job]) as mocked:
                    frame = scrape_jobs(
                        site_name="indeed", enforce_annual_salary=annualise, verbose=0
                    )
                mocked.assert_called_once()
                row = frame.iloc[0]
                self.assertEqual(row["salary_origin"], "employer")
                self.assertEqual(row["salary_source"], "direct_data")
                self.assertEqual(row["interval"], interval)
                self.assertEqual(row["min_amount"], minimum)
                self.assertEqual(row["max_amount"], minimum * 2)
                self.assertEqual(row["currency"], "PLN")
                self.assertIsInstance(row["salary_evidence"], dict)
                self.assertEqual(row["salary_evidence"], original["salary_evidence"])
                with tempfile.TemporaryDirectory() as directory:
                    for suffix in ("json", "jsonl"):
                        path = Path(directory) / f"jobs.{suffix}"
                        save(frame, str(path))
                        loaded = json.loads(path.read_text())
                        exported = loaded[0] if suffix == "json" else loaded
                        self.assertEqual(
                            exported["salary_evidence"], original["salary_evidence"]
                        )
                        self.assertEqual(exported["salary_origin"], "employer")
                        self.assertEqual(exported["min_amount"], minimum)
                        self.assertEqual(exported["title"], "Python developer")
                        self.assertEqual(exported["site"], "indeed")
                        self.assertEqual(exported["salary_source"], "direct_data")
                self.assertEqual(job.model_dump(), original)
        for guard in self.network_guards:
            guard.assert_not_called()

    def test_other_portals_do_not_infer_employer_from_direct_data(self):
        job = JobPost(
            job_url="https://example.invalid/job",
            title="Other portal",
            min_amount=100,
            max_amount=200,
            currency="PLN",
            interval=CompensationInterval.MONTHLY,
        )
        for site, scraper in SCRAPER_MAPPING.items():
            if site == Site.INDEED:
                continue
            with self.subTest(site=site.value):
                with patch.object(scraper, "scrape", return_value=[job]) as mocked:
                    frame = scrape_jobs(site_name=site, verbose=0)
                mocked.assert_called_once()
                self.assertEqual(frame.iloc[0]["salary_source"], "direct_data")
                self.assertEqual(frame.iloc[0]["salary_origin"], "unknown")
                self.assertIsNone(frame.iloc[0]["salary_evidence"])

    def test_network_guard_blocks_requests_and_native_curl(self):
        for request in (requests.get, curl_cffi.requests.get):
            with self.subTest(transport=request.__module__):
                with self.assertRaisesRegex(AssertionError, "Network forbidden"):
                    request("https://example.invalid")
        with self.assertRaisesRegex(AssertionError, "Network forbidden"):
            socket.create_connection(("example.invalid", 443))


if __name__ == "__main__":
    unittest.main()
