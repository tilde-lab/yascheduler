#!/usr/bin/env python
"""
Yascheduler systemd daemon
"""

if __name__ == "__main__":
    from yascheduler import LOG_FILE
    from yascheduler.scheduler import daemonize
    daemonize(log_file=LOG_FILE)
