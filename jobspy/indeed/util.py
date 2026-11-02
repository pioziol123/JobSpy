from __future__ import annotations

from jobspy.indeed.constant import (
    full_time_is_permanent_in,
    hybrid_key,
    job_types_by_code,
    pay_intervals,
    permanent_key,
    remote_key,
)
from jobspy.indeed.model import Attribute, IndeedCompensation
from jobspy.model import Compensation, Country, JobType, SalaryOrigin


def get_job_type(attributes: list[Attribute], country: Country) -> list[JobType] | None:
    """
    The job types among a job's attributes; `country` is the one searched
    """
    job_types: list[JobType] = []
    for attribute in attributes:
        job_type = job_types_by_code.get(attribute.key) if attribute.key else None
        if attribute.key == permanent_key and country == full_time_is_permanent_in:
            job_type = JobType.FULL_TIME
        if job_type and job_type not in job_types:
            job_types.append(job_type)
    return job_types or None


def get_compensation(compensation: IndeedCompensation) -> Compensation:
    """
    The employer's pay, else Indeed's own estimate
    """
    pay = compensation.baseSalary
    currency = compensation.currencyCode
    origin: SalaryOrigin = "employer"
    evidence = (
        compensation.model_dump(mode="json")
        if compensation.baseSalary is not None
        or compensation.currencyCode is not None
        or (
            compensation.estimated is not None
            and (
                compensation.estimated.baseSalary is not None
                or compensation.estimated.currencyCode is not None
            )
        )
        else None
    )
    if not pay and compensation.estimated:
        pay = compensation.estimated.baseSalary
        currency = compensation.estimated.currencyCode
        origin = "portal_estimate"
    interval = pay_intervals.get(pay.unitOfWork) if pay and pay.unitOfWork else None
    if not pay or not interval:
        return Compensation(salary_evidence=evidence)
    amounts = pay.range
    min_amount = amounts.min if amounts.min is not None else amounts.value
    max_amount = amounts.max if amounts.max is not None else amounts.value
    return Compensation(
        interval=interval,
        min_amount=round(min_amount, 2) if min_amount is not None else None,
        max_amount=round(max_amount, 2) if max_amount is not None else None,
        currency=currency,
        salary_origin=origin
        if min_amount is not None or max_amount is not None
        else "unknown",
        salary_evidence=evidence,
    )


def is_job_remote(attributes: list[Attribute]) -> bool:
    """
    Indeed's remote attribute, unless the job is also tagged hybrid
    """
    keys = {attribute.key for attribute in attributes}
    return remote_key in keys and hybrid_key not in keys
