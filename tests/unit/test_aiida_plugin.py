# region MODULE_CONTRACT
# PURPOSE: Unit tests for the requested-jobs poll guard in the AiiDA scheduler plugin.
# SCOPE: _check_requested_jobs_present - unreported requested jobs raise, fully reported lists pass, malformed job ids are contained.
# KEYWORDS: aiida plugin, get_jobs, poll guard, transient failure
# endregion MODULE_CONTRACT

from __future__ import annotations

import pytest

pytest.importorskip("aiida")

from aiida.schedulers.datastructures import JobInfo, JobState

from yascheduler.entrypoints.aiida_plugin import (
    YaScheduler,
    _MissingRequestedJobsError,
)

pytestmark = pytest.mark.unit


def _job(job_id: str) -> JobInfo:
    job = JobInfo()
    job.job_id = job_id
    job.job_state = JobState.QUEUED
    return job


def test_all_requested_jobs_reported_passes():
    YaScheduler._check_requested_jobs_present(
        ["5000", "5001"],
        [_job("5000"), _job("5001")],
        0,
        "5000 TO_DO\n5001 TO_DO",
        "",
    )


def test_single_string_requested_job_reported_passes():
    YaScheduler._check_requested_jobs_present(
        "5000", [_job("5000")], 0, "5000 TO_DO", ""
    )


def test_empty_output_raises_with_stderr_in_message():
    with pytest.raises(_MissingRequestedJobsError, match="connection refused"):
        YaScheduler._check_requested_jobs_present(
            "5000", [], 1, "", "connection refused"
        )


def test_partial_output_raises():
    with pytest.raises(_MissingRequestedJobsError):
        YaScheduler._check_requested_jobs_present(
            ["5000", "5001"], [_job("5000")], 0, "5000 TO_DO", ""
        )


def test_malformed_requested_job_is_ignored():
    YaScheduler._check_requested_jobs_present(
        ["", "not-a-task-id", "5000"],
        [_job("5000")],
        0,
        "5000 TO_DO",
        "",
    )


def test_malformed_requested_jobs_alone_do_not_raise():
    YaScheduler._check_requested_jobs_present("", [], 1, "", "connection refused")
