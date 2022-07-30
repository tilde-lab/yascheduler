#!/usr/bin/env bash
# Pcrystal **Debian 10** installer

CURDIR=$(dirname "$0")
MACHINEFILE=$CURDIR/nodes
mapfile -t MACHINES < <(cat "$MACHINEFILE")

# shellcheck disable=SC1090,SC1091
. "$CURDIR/read_ini.sh"
read_ini /etc/yascheduler/yascheduler.conf

# shellcheck disable=SC2154
# BIN_TO_COPY=${INI__local__deployable_code}
# shellcheck disable=SC2154
WWW_TO_COPY=${INI__local__deployable_code}
TEST_TO_COPY=$CURDIR/data/CRYSTAL-benchmark/INPUT

for ((i = 0; i < ${#MACHINES[@]}; i++)); do
	echo "Setup ${MACHINES[i]}"
	ssh -T -o UserKnownHostsFile=~/.ssh/known_hosts -o StrictHostKeyChecking=no "${MACHINES[i]}" "whoami"

	# Manage packages
	ssh -T "${MACHINES[i]}" "apt-get -y update && apt-get -y upgrade" >/dev/null
	ssh -T "${MACHINES[i]}" "apt-get -y install wget openmpi-bin" >/dev/null

	# Show versions
	ssh -T "${MACHINES[i]}" "/usr/bin/mpirun --allow-run-as-root -V"
	ssh -T "${MACHINES[i]}" "cat /etc/issue"
	ssh -T "${MACHINES[i]}" "grep -c ^processor /proc/cpuinfo"

	# Copy exec
	ssh -T "${MACHINES[i]}" "mkdir -p /root/bin"
	#scp $BIN_TO_COPY ${MACHINES[i]}:/root/bin
	ssh -T "${MACHINES[i]}" "cd /root/bin && wget $WWW_TO_COPY"
	ssh -T "${MACHINES[i]}" "cd /root/bin && tar xvf *.gz"
	ssh -T "${MACHINES[i]}" "ln -s /root/bin/Pcrystal /usr/bin/Pcrystal"
	echo "set mouse-=a" >~/.vimrc

	# Benchmark
	ssh -T "${MACHINES[i]}" "mkdir -p /data/benchmark"
	scp "$TEST_TO_COPY" "${MACHINES[i]}":/data/benchmark
	#ssh -T ${MACHINES[i]} "nohup /usr/bin/mpirun -np 8 --allow-run-as-root -wd /data/benchmark /usr/bin/Pcrystal > /data/benchmark/OUTPUT 2>&1 &"

done
