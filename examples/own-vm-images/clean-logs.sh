#!/usr/bin/env bash
# remove logs

journalctl --flush
journalctl --rotate --vacuum-time=0

find /var/log -type f -exec truncate --size 0 {} \; # truncate system logs
find /var/log -type f -name '*.[1-9]' -delete # remove archived logs
find /var/log -type f -name '*.gz' -delete # remove compressed archived logs
