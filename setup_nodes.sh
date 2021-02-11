#!/bin/bash
# Pcrystal **Debian 10** installer

MACHINEFILE=$(dirname "$0")/nodes
MACHINES=($( cat $MACHINEFILE ))

. $(dirname $0)/read_ini.sh
read_ini /etc/yascheduler/yascheduler.conf

BIN_TO_COPY=${INI__local__deployable_code}
WWW_TO_COPY=${INI__local__deployable_code}

for (( i=0; i<${#MACHINES[@]}; i++ )); do
    echo "Setup ${MACHINES[i]}"
    ssh -T -o UserKnownHostsFile=~/.ssh/known_hosts -o StrictHostKeyChecking=no ${MACHINES[i]} "whoami"

    # Manage packages
    ssh -T ${MACHINES[i]} "apt-get -y update && apt-get -y upgrade" > /dev/null
    ssh -T ${MACHINES[i]} "apt-get -y install wget openmpi-bin" > /dev/null

    # Show versions
    ssh -T ${MACHINES[i]} "/usr/bin/mpirun --allow-run-as-root -V"
    ssh -T ${MACHINES[i]} "cat /etc/issue"
    ssh -T ${MACHINES[i]} "grep -c ^processor /proc/cpuinfo"

    # Copy exec
    ssh -T ${MACHINES[i]} "mkdir -p /root/bin"
    #scp $BIN_TO_COPY ${MACHINES[i]}:/root/bin
    ssh -T ${MACHINES[i]} "cd /root/bin && wget $WWW_TO_COPY"
    ssh -T ${MACHINES[i]} "cd /root/bin && tar xvf *.gz"
    ssh -T ${MACHINES[i]} "ln -s /root/bin/Pcrystal /usr/bin/Pcrystal"
    echo "set mouse-=a" > ~/.vimrc

done
