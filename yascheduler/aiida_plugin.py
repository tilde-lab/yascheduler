"""
Aiida plugin for yascheduler
"""

import aiida.schedulers
from aiida.schedulers.datastructures import (JobState, JobInfo, NodeNumberJobResource)

_MAP_STATUS_YASCHEDULER = {
    'QUEUED': JobState.QUEUED,
    'RUNNING': JobState.RUNNING,
    'FINISHED': JobState.DONE
}


class YaschedJobResource(NodeNumberJobResource):

    def __init__(self, *_, **kwargs):
        super(YaschedJobResource, self).__init__(**kwargs)


class YaScheduler(aiida.schedulers.Scheduler):
    """
    Support for the YaScheduler designed specifically for MPDS
    """
    _logger = aiida.schedulers.Scheduler._logger.getChild('yascheduler')

    # Query only by list of jobs and not by user
    _features = {
        'can_query_by_user': False,
    }

    # The class to be used for the job resource.
    _job_resource_class = YaschedJobResource

    def _get_joblist_command(self, jobs=None, user=None):
        """
        The command to report full information on existing jobs.
        """
        from aiida.common.exceptions import FeatureNotAvailable
        if user:
            raise FeatureNotAvailable("Cannot query by user in Yascheduler")
        command = ["yastatus"]
        # make list from job ids (taken from slurm scheduler)
        if jobs:
            joblist = []
            if isinstance(jobs, str):
                joblist.append(jobs)
            else:
                if not isinstance(jobs, (tuple, list)):
                    raise TypeError("If provided, the 'jobs' variable must be a string or a list of strings")
                joblist = jobs
            command.append('--jobs {}'.format(' '.join(joblist)))
        return ' '.join(command)

    def _get_detailed_jobinfo_command(self, jobid):
        """
        Return the command to run to get the detailed information on a job,
        even after the job has finished.
        """
        return 'yastatus --jobs {}'.format(jobid)

    def _get_submit_script_header(self, job_tmpl):
        """
        Return the submit script header, using the parameters from the
        job_tmpl.
        """
        lines = []
        if job_tmpl.job_name:
            lines.append("LABEL={}".format(job_tmpl.job_name))

        # TODO too specific for engine.pcrystal
        lines += ["INPUT=INPUT", "STRUCT=fort.34"]
        return "\n".join(lines)

    def _get_submit_command(self, submit_script):
        """
        Return the string to execute to submit a given script.
        """
        return 'yasubmit {}'.format(submit_script)

    def _parse_submit_output(self, retval, stdout, stderr):
        """
        Parse the output of the submit command, as returned by executing the
        command returned by _get_submit_command command.
        """
        if stderr.strip():
            self.logger.warning("Stderr when submitting: {}".format(stderr.strip()))
        return stdout.split(':')[1].strip()

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
            self.logger.warning("Stderr when parsing joblist: {}".format(stderr.strip()))
        job_list = [job.split() for job in stdout.split('\n') if job]
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

    def _parse_kill_output(self, retval, stdout, stderr):
        """
        Parse the output of the kill command.
        """