"""
Aiida plugin for yascheduler
with respect to the supported yascheduler engines.
"""

import aiida.schedulers  # pylint: disable=import-error
from aiida.orm import load_node  # pylint: disable=import-error
# pylint: disable=import-error
from aiida.schedulers.datastructures import (
    JobInfo,
    JobState,
    NodeNumberJobResource,
)
import requests

from .config import Config
from .variables import CONFIG_FILE

_MAP_STATUS_YASCHEDULER = {
    "TO_DO": JobState.QUEUED,
    "RUNNING": JobState.RUNNING,
    "DONE": JobState.DONE,
}

# NB under virtualenv, this should refer to virtualenv's /bin/
_CMD_PREFIX = ""


class YaschedJobResource(NodeNumberJobResource):
    def __init__(self, *_, **kwargs):
        super().__init__(**kwargs)


class YaScheduler(aiida.schedulers.Scheduler):
    """Support for the YaScheduler designed specifically for MPDS."""

    _logger = aiida.schedulers.Scheduler._logger.getChild("yascheduler")

    # Query only by list of jobs and not by user
    _features = {
        "can_query_by_user": False,
    }

    # The class to be used for the job resource.
    _job_resource_class = YaschedJobResource

    def _get_joblist_command(self, jobs=None, user=None):
        """The command to report full information on existing jobs."""
        # pylint: disable=import-error
        from aiida.common.exceptions import FeatureNotAvailable

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
                    raise TypeError(
                        "If provided, the 'jobs' variable \
must be a string or a list of strings"
                    )
                joblist = jobs
            command.append("--jobs {}".format(" ".join(joblist)))
        return " ".join(command)

    def _get_detailed_jobinfo_command(self, jobid):
        """
        Return the command to run to get the detailed information on a job,
        even after the job has finished.
        """
        return f"{_CMD_PREFIX}yastatus --jobs {jobid}"

    def _get_submit_script_header(self, job_tmpl):
        """
        Return the submit script header, using the parameters from the
        job_tmpl.
        """
        assert job_tmpl.job_name
        # There is no other way to get the
        # code label and the WF uuid except this (TODO?)
        pk = int(job_tmpl.job_name.split("-")[1])
        aiida_node = load_node(pk)

        # We map the lowercase code labels onto yascheduler engines,
        # so that the required input file(s) can be deduced
        lines = [f"ENGINE={aiida_node.inputs.code.label.lower()}"]

        config = Config.from_config_parser(CONFIG_FILE)
        wh_url = config.local.webhook_url
        if wh_url:
            self.intercept_task_submission(aiida_node, wh_url)

        try:
            lines.append(f"PARENT={aiida_node.caller.uuid}")
        except AttributeError:
            pass

        lines.append(f"LABEL={job_tmpl.job_name}")
        return "\n".join(lines)

    def _get_submit_command(self, submit_script):
        """Return the string to execute to submit a given script."""
        return f"{_CMD_PREFIX}yasubmit {submit_script}"

    def _parse_submit_output(self, retval, stdout, stderr):
        """
        Parse the output of the submit command, as returned by executing the
        command returned by _get_submit_command command.
        """
        if stderr.strip():
            self.logger.warning(f"Stderr while submitting: {stderr.strip()}")

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
            self.logger.warning(f"Stderr when parsing joblist: \
{stderr.strip()}")
        job_list = [job.split() for job in stdout.split("\n") if job]
        job_infos = []
        for job_id, status in job_list:
            job = JobInfo()
            job.job_id = job_id
            job.job_state = _MAP_STATUS_YASCHEDULER[status]
            job_infos.append(job)
        return job_infos

    def _get_kill_command(self, jobid):
        """Return the command to kill the job with specified jobid."""

    def _parse_kill_output(self, retval, stdout, stderr):
        """Parse the output of the kill command."""

    def _send_webhook(self, webhook_url, **argv):
        """Send task information to the server via a webhook."""
        params = {
            'payload': argv['payload'],
            'status': argv['status'],
        }

        response = requests.get(webhook_url, params=params)
        if response.status_code != 200:
            self.logger.error(f"Webhook received an error: \
{response.status_code}, {response.text}")

    def _prepare_task_data(self, aiida_node):
        """Prepare the task information for the webhook."""
        return {
            'payload': aiida_node.label,
            'status': _process_status(aiida_node)
        }

    def intercept_task_submission(self, aiida_node, webhook_url):
        """Handle the task submission, preparing
        data and sending it to the webhook.
        """
        data = self._prepare_task_data(aiida_node)
        self._send_webhook(webhook_url=webhook_url, **data)


def _process_status(node) -> str:
    """Receive correct node status from node."""
    status = node.process_state.value

    if status.lower() == "finished":
        status += f'-{node.exit_code.status}' if node.exit_code else "-0"

    return status
