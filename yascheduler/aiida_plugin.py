"""
Aiida plugin for yascheduler,
with respect to the supported yascheduler engines
"""

import aiida.schedulers
from aiida.common.escaping import escape_for_bash
from aiida.common.exceptions import FeatureNotAvailable
from aiida.orm import load_node
from aiida.schedulers.datastructures import JobInfo, JobState, NodeNumberJobResource
from aiida.schedulers.scheduler import SchedulerError

_MAP_STATUS_YASCHEDULER = {
    "TO_DO": JobState.QUEUED,
    "RUNNING": JobState.RUNNING,
    "DONE": JobState.DONE,
}
_CMD_PREFIX = ""  # NB under virtualenv, this should refer to virtualenv's /bin/


class YaschedJobResource(NodeNumberJobResource):
    def __init__(self, *_, **kwargs):
        super().__init__(**kwargs)


class YaScheduler(aiida.schedulers.Scheduler):
    """
    Support for the YaScheduler designed specifically for MPDS
    """

    _logger = aiida.schedulers.Scheduler._logger.getChild("yascheduler")

    # Query only by list of jobs and not by user
    _features = {
        "can_query_by_user": False,
    }

    # The class to be used for the job resource.
    _job_resource_class = YaschedJobResource

    def submit_job(self, working_directory, filename):
        """
        Submit a job script to yascheduler.

        AiiDA 2.7 makes this public method abstract on the base Scheduler.
        Older AiiDA versions provided the same behavior through submit_from_script.
        """
        self.transport.chdir(working_directory)
        result = self.transport.exec_command_wait(self._get_submit_command(escape_for_bash(filename)))
        return self._parse_submit_output(*result)

    def get_jobs(self, jobs=None, user=None, as_dict=False):
        """
        Return the list of currently active jobs.

        AiiDA 2.7 makes this public method abstract on the base Scheduler.
        """
        with self.transport:
            retval, stdout, stderr = self.transport.exec_command_wait(self._get_joblist_command(jobs=jobs, user=user))

        joblist = self._parse_joblist_output(retval, stdout, stderr)
        if as_dict:
            jobdict = {job.job_id: job for job in joblist}
            if None in jobdict:
                raise SchedulerError("Found at least one job without jobid")
            return jobdict

        return joblist

    def kill_job(self, jobid):
        """
        Report that job cancellation is not supported by yascheduler.

        The CLI currently exposes status and submit commands, but no task
        cancellation command. Returning False lets AiiDA handle this as an
        unsuccessful kill without pretending the remote task was stopped.
        """
        self.logger.warning(f"Job cancellation is not supported by yascheduler: {jobid}")
        return False

    def _get_joblist_command(self, jobs=None, user=None):
        """
        The command to report full information on existing jobs.
        """

        if user:
            raise FeatureNotAvailable("Cannot query by user in Yascheduler")
        command = [f"{_CMD_PREFIX}yastatus"]
        # make list from job ids (taken from slurm scheduler)
        if jobs:
            joblist = []
            if isinstance(jobs, str):
                joblist.append(jobs)
            else:
                if not isinstance(jobs, (tuple, list)):
                    raise TypeError("If provided, the 'jobs' variable must be a string or a list of strings")
                joblist = jobs
            command.append("--jobs {}".format(" ".join(joblist)))
        return " ".join(command)

    def _get_detailed_jobinfo_command(self, jobid):
        """
        Return the command to run to get the detailed information on a job,
        even after the job has finished.
        """
        return f"{_CMD_PREFIX}yastatus --jobs {jobid}"

    def _get_detailed_job_info_command(self, job_id):
        """
        Return the command to run to get detailed information on a job.

        This is the method name expected by AiiDA. Keep the older misspelled
        variant above as an alias for any external callers.
        """
        return self._get_detailed_jobinfo_command(job_id)

    def _get_submit_script_header(self, job_tmpl):
        """
        Return the submit script header, using the parameters from the
        job_tmpl.
        """
        assert job_tmpl.job_name
        # There is no other way to get the code label and the WF uuid except this (TODO?)
        pk = int(job_tmpl.job_name.split("-")[1])
        aiida_node = load_node(pk)

        # We map the lowercase code labels onto yascheduler engines,
        # so that the required input file(s) can be deduced
        lines = [f"ENGINE={aiida_node.inputs.code.label.lower()}"]

        try:
            lines.append(f"PARENT={aiida_node.caller.uuid}")
        except AttributeError:
            pass

        lines.append(f"LABEL={job_tmpl.job_name}")
        return "\n".join(lines)

    def _get_submit_command(self, submit_script):
        """
        Return the string to execute to submit a given script.
        """
        return f"{_CMD_PREFIX}yasubmit {submit_script}"

    def _parse_submit_output(self, retval, stdout, stderr):
        """
        Parse the output of the submit command, as returned by executing the
        command returned by _get_submit_command command.
        """
        if stderr.strip():
            self.logger.warning(f"Stderr when submitting: {stderr.strip()}")

        output = stdout.strip()

        try:
            int(output)
        except ValueError:
            self.logger.error("Submitting failed, no task id received")

        return output

    def _parse_joblist_output(self, retval, stdout, stderr):
        """
        Parse the queue output string, as returned by executing the
        command returned by _get_joblist_command command,
        that is here implemented as a list of lines, one for each
        job, with _field_separator as separator. The order is described
        in the _get_joblist_command function.
        Return a list of JobInfo objects, one of each job,
        each relevant parameters implemented.
        """
        if stderr.strip():
            self.logger.warning(f"Stderr when parsing joblist: {stderr.strip()}")
        job_list = [job.split() for job in stdout.split("\n") if job]
        job_infos = []
        for job_id, status in job_list:
            job = JobInfo()
            job.job_id = job_id
            job.job_state = _MAP_STATUS_YASCHEDULER[status]
            job_infos.append(job)
        return job_infos

    def _get_kill_command(self, jobid):
        """
        Return the command to kill the job with specified jobid.
        """
        raise FeatureNotAvailable("Job cancellation is not supported by Yascheduler")

    def _parse_kill_output(self, retval, stdout, stderr):
        """
        Parse the output of the kill command.
        """
        return False
