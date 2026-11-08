"""AiiDA scheduler plugin entry point for yascheduler."""
# region MODULE_CONTRACT
# PURPOSE: Integrate yascheduler with AiiDA as a scheduler plugin — implements the AiiDA Scheduler base class so workflows can submit, query, and manage yascheduler tasks through AiiDA's transport layer.
# SCOPE: YaScheduler AiiDA Scheduler subclass, YaschedJobResource, internal helper classes, and status mapping.
# DEPENDENCIES: aiida.schedulers, aiida.orm, aiida.common.
# KEYWORDS: aiida, scheduler, plugin, integration, transport
# endregion MODULE_CONTRACT

from __future__ import annotations

import contextlib
from typing import Any, NoReturn

import aiida.schedulers
from aiida.common.escaping import escape_for_bash
from aiida.common.exceptions import FeatureNotAvailable
from aiida.orm import load_node
from aiida.schedulers.datastructures import (
    JobInfo,
    JobState,
    JobTemplate,
    NodeNumberJobResource,
)
from aiida.schedulers.scheduler import SchedulerError

__all__ = ["YaScheduler", "YaschedJobResource"]


class _JobNotFoundError(SchedulerError):
    def __init__(self) -> None:
        super().__init__("Found at least one job without jobid")


class _QueryByUserNotAvailableError(FeatureNotAvailable):
    def __init__(self) -> None:
        super().__init__("Cannot query by user in Yascheduler")


class _InvalidJobsTypeError(TypeError):
    def __init__(self) -> None:
        super().__init__(
            "If provided, the 'jobs' variable must be a string or a list of strings",
        )


class _JobCancellationNotSupportedError(FeatureNotAvailable):
    def __init__(self) -> None:
        super().__init__("Job cancellation is not supported by Yascheduler")


_MAP_STATUS_YASCHEDULER = {
    "TO_DO": JobState.QUEUED,
    "RUNNING": JobState.RUNNING,
    "DONE": JobState.DONE,
}
_CMD_PREFIX = ""  # NB under virtualenv, this should refer to virtualenv's /bin/


class YaschedJobResource(NodeNumberJobResource):
    """Resource class for yascheduler jobs in AiiDA."""

    def __init__(self, *_: list[Any], **kwargs: dict[str, Any]) -> None:
        super().__init__(**kwargs)


class YaScheduler(aiida.schedulers.Scheduler):
    """Support for the YaScheduler designed specifically for MPDS."""

    _logger = aiida.schedulers.Scheduler._logger.getChild("yascheduler")  # noqa: SLF001

    # Query only by list of jobs and not by user
    _features = {  # noqa: RUF012
        "can_query_by_user": False,
    }

    # The class to be used for the job resource.
    _job_resource_class = YaschedJobResource

    # region METHOD_submit_job
    # PURPOSE: Submit a job script to yascheduler via transport, returning the job ID.
    # REQUIRES: working_directory and filename pointing to an existing remote script.
    def submit_job(self, working_directory: str, filename: str) -> str:
        """Submit a job script to yascheduler.

        AiiDA 2.7 makes this public method abstract on the base Scheduler.
        Older AiiDA versions provided the same behavior through submit_from_script.
        """
        self.transport.chdir(working_directory)
        result = self.transport.exec_command_wait(
            self._get_submit_command(escape_for_bash(filename)),
        )
        return self._parse_submit_output(*result)

    # endregion METHOD_submit_job

    # region METHOD_get_jobs
    # PURPOSE: Return list of currently active jobs from yascheduler, filtering by job IDs or user.
    def get_jobs(
        self,
        jobs: str | list[str] | None = None,
        user: str | None = None,
        as_dict: bool = False,
    ) -> list[JobInfo] | dict[str, JobInfo]:
        """Return the list of currently active jobs.

        AiiDA 2.7 makes this public method abstract on the base Scheduler.
        """
        with self.transport:
            retval, stdout, stderr = self.transport.exec_command_wait(
                self._get_joblist_command(jobs=jobs, user=user),
            )

        joblist = self._parse_joblist_output(retval, stdout, stderr)
        if jobs is not None:
            self._check_requested_jobs_present(jobs, joblist, retval, stdout)
        if as_dict:
            jobdict = {job.job_id: job for job in joblist}
            if None in jobdict:
                raise _JobNotFoundError
            return jobdict

        return joblist

    # endregion METHOD_get_jobs

    # region METHOD__check_requested_jobs_present
    # PURPOSE: Guard the poll against transient yastatus failures silently killing queued AiiDA calculations.
    @staticmethod
    def _check_requested_jobs_present(
        requested: str | list[str],
        joblist: list[JobInfo],
        retval: int,
        stdout: str,
    ) -> None:
        """Raise if any explicitly requested job is missing from the parsed list.

        A transient yastatus failure (scheduler or database restarting, CLI
        configuration error) yields an empty or partial job list. AiiDA's
        polling contract treats a job absent from the list as DONE, so a
        queued calculation would be 'retrieved' with no output and fail
        spuriously. Raising instead makes AiiDA retry the poll and keeps the
        calculation waiting.
        """
        wanted = [requested] if isinstance(requested, str) else list(requested)
        reported = {job.job_id for job in joblist}
        missing = [job for job in wanted if job not in reported]
        if missing:
            raise SchedulerError(
                f"yastatus did not report requested job(s) {missing} "
                f"(retval={retval}, stdout={stdout.strip()[:200]!r}): refusing to "
                "interpret unreported jobs as finished - scheduler or database "
                "may be transiently unavailable"
            )

    # endregion METHOD__check_requested_jobs_present

    # region METHOD_kill_job
    # PURPOSE: Report that job cancellation is not supported by yascheduler (always returns False).
    def kill_job(self, jobid: str) -> bool:
        """Report that job cancellation is not supported by yascheduler."""
        self.logger.warning(
            "Job cancellation is not supported by yascheduler: %s",
            jobid,
        )
        return False

    # endregion METHOD_kill_job

    # region METHOD__get_joblist_command
    # PURPOSE: Build the shell command to query job status from yascheduler.
    def _get_joblist_command(
        self,
        jobs: str | list[str] | None = None,
        user: str | None = None,
    ) -> str:
        """Return the command to report full information on existing jobs."""
        if user:
            raise _QueryByUserNotAvailableError
        command = [f"{_CMD_PREFIX}yastatus"]
        # make list from job ids (taken from slurm scheduler)
        if jobs:
            joblist = []
            if isinstance(jobs, str):
                joblist.append(jobs)
            else:
                if not isinstance(jobs, (tuple, list)):
                    raise _InvalidJobsTypeError
                joblist = jobs
            command.append("--jobs {}".format(" ".join(joblist)))
        return " ".join(command)

    # endregion METHOD__get_joblist_command

    # region METHOD__get_detailed_jobinfo_command
    # PURPOSE: Build shell command to get detailed info for a specific job.
    def _get_detailed_jobinfo_command(self, jobid: str) -> str:
        """Return the command to run to get the detailed information on a job.

        Even after the job has finished.
        """
        return f"{_CMD_PREFIX}yastatus --jobs {jobid}"

    # endregion METHOD__get_detailed_jobinfo_command

    # region METHOD__get_detailed_job_info_command
    # PURPOSE: Delegate to _get_detailed_jobinfo_command (AiiDA expected method name).
    def _get_detailed_job_info_command(self, job_id: str) -> str:
        """Return the command to run to get detailed information on a job.

        This is the method name expected by AiiDA. Keep the older misspelled
        variant above as an alias for any external callers.
        """
        return self._get_detailed_jobinfo_command(job_id)

    # endregion METHOD__get_detailed_job_info_command

    # region METHOD__get_submit_script_header
    # PURPOSE: Generate the submit script header with ENGINE and LABEL variables from an AiiDA job template.
    def _get_submit_script_header(self, job_tmpl: JobTemplate) -> str:
        """Return the submit script header.

        Using the parameters from the job_tmpl.
        """
        assert job_tmpl.job_name
        # There is no other way to get the code label and the WF uuid except this (TODO?)
        pk = int(job_tmpl.job_name.split("-")[1])
        aiida_node = load_node(pk)

        # We map the lowercase code labels onto yascheduler engines,
        # so that the required input file(s) can be deduced
        lines = [f"ENGINE={aiida_node.inputs.code.label.lower()}"]

        with contextlib.suppress(AttributeError):
            lines.append(f"PARENT={aiida_node.caller.uuid}")

        lines.append(f"LABEL={job_tmpl.job_name}")
        return "\n".join(lines)

    # endregion METHOD__get_submit_script_header

    # region METHOD__get_submit_command
    # PURPOSE: Build the shell command to submit a script via yasubmit.
    def _get_submit_command(self, submit_script: str) -> str:
        """Return the string to execute to submit a given script."""
        return f"{_CMD_PREFIX}yasubmit {submit_script}"

    # endregion METHOD__get_submit_command

    # region METHOD__parse_submit_output
    # PURPOSE: Parse the output of the yasubmit command to extract task ID.
    def _parse_submit_output(self, retval: int, stdout: str, stderr: str) -> str:  # noqa: ARG002
        """Parse the output of the submit command."""
        if stderr.strip():
            self.logger.warning("Stderr when submitting: %s", stderr.strip())

        output = stdout.strip()

        try:
            int(output)
        except ValueError:
            self.logger.exception("Submitting failed, no task id received")

        return output

    # endregion METHOD__parse_submit_output

    # region METHOD__parse_joblist_output
    # PURPOSE: Parse yastatus output into JobInfo objects.
    def _parse_joblist_output(
        self,
        retval: int,  # noqa: ARG002
        stdout: str,
        stderr: str,
    ) -> list[JobInfo]:
        """Parse the queue output string from yastatus."""
        if stderr.strip():
            self.logger.warning("Stderr when parsing joblist: %s", stderr.strip())
        job_list = [job.split() for job in stdout.split("\n") if job]
        job_infos = []
        for job_id, status in job_list:
            job = JobInfo()
            job.job_id = job_id
            job.job_state = _MAP_STATUS_YASCHEDULER[status]
            job_infos.append(job)
        return job_infos

    # endregion METHOD__parse_joblist_output

    # region METHOD__get_kill_command
    # PURPOSE: Report that job kill is not supported (raises FeatureNotAvailable).
    def _get_kill_command(self, jobid: str) -> NoReturn:  # noqa: ARG002
        """Return the command to kill the job with specified jobid."""
        raise _JobCancellationNotSupportedError

    # endregion METHOD__get_kill_command

    # region METHOD__parse_kill_output
    # PURPOSE: Return False indicating kill was not performed.
    def _parse_kill_output(self, retval: int, stdout: str, stderr: str) -> bool:  # noqa: ARG002
        """Parse the output of the kill command."""
        return False

    # endregion METHOD__parse_kill_output
