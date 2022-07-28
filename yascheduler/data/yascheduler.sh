#!/bin/bash
#
# yascheduler      Startup script for yascheduler
#
# chkconfig: - 87 12
# description: init.d daemon for yet another scheduler
# config: /etc/yascheduler/yascheduler.conf
# config: /etc/sysconfig/yascheduler
# pidfile: /var/run/yascheduler.pid
#
### BEGIN INIT INFO
# Provides: yascheduler
# Required-Start: $local_fs
# Required-Stop: $local_fs
# Short-Description: start and stop yascheduler server
# Description: yascheduler stands for yet another scheduler
### END INIT INFO

# Source function library.
# shellcheck disable=SC1091
. /etc/rc.d/init.d/functions

if [ -f /etc/sysconfig/yascheduler ]; then
	. /etc/sysconfig/yascheduler
fi

yascheduler=%YASCHEDULER_DAEMON_FILE%
prog=yascheduler
pidfile=${PIDFILE-/var/run/yascheduler.pid}
logfile=${LOGFILE-/var/log/yascheduler.log}
RETVAL=0

OPTIONS=""

start() {
	echo -n $"Starting $prog: "

	if [[ -f ${pidfile} ]]; then
		pid=$(cat "$pidfile")
		isrunning=$(pgrep -F "$pidfile" 2>/dev/null)

		if [[ -n ${isrunning} ]]; then
			echo $"$prog already running"
			return 0
		fi
	fi
	$yascheduler -p "$pidfile" -l "$logfile" "$OPTIONS"
	RETVAL=$?
	if [ $RETVAL = 0 ]; then success; else failure; fi
	echo
	return $RETVAL
}

stop() {
	if [[ -f ${pidfile} ]]; then
		pid=$(cat "$pidfile")
		isrunning=$(pgrep -F "$pidfile" 2>/dev/null)

		if [[ ${isrunning} -eq ${pid} ]]; then
			echo -n $"Stopping $prog: "
			kill "$pid"
		else
			echo -n $"Stopping $prog: "
			success
		fi
		RETVAL=$?
	fi
	echo
	return $RETVAL
}

reload() {
	echo -n $"Reloading $prog: "
	echo
}

# See how we were called.
case "$1" in
start)
	start
	;;
stop)
	stop
	;;
status)
	status -p "$pidfile" $yascheduler
	RETVAL=$?
	;;
restart)
	stop
	start
	;;
force-reload | reload)
	reload
	;;
*)
	echo $"Usage: $prog {start|stop|restart|force-reload|reload|status}"
	RETVAL=2
	;;
esac

exit $RETVAL
