#!/bin/bash
# Pcrystal **Debian 10** installer

MACHINEFILE=$(dirname "$0")/nodes
MACHINES=($( cat $MACHINEFILE ))

BIN_TO_COPY=~/crystal_tests/CRYSTAL_bin/debian_10_openmpi_3.1.3/Pcrystal
TEST_TO_COPY=~/crystal_tests/universal_test_input/INPUT

for (( i=0; i<${#MACHINES[@]}; i++ )); do
    echo "Setup ${MACHINES[i]}"

    # Manage packages
    ssh -T ${MACHINES[i]} "apt-get -y update && apt-get -y upgrade" > /dev/null
    ssh -T ${MACHINES[i]} "apt-get -y install openmpi-bin" > /dev/null

    # Show versions
    ssh -T ${MACHINES[i]} "/usr/bin/mpirun --allow-run-as-root -V"
    ssh -T ${MACHINES[i]} "cat /etc/issue"
    ssh -T ${MACHINES[i]} "grep -c ^processor /proc/cpuinfo"

    # Copy exec
    ssh -T ${MACHINES[i]} "mkdir -p /root/bin"
    scp $BIN_TO_COPY ${MACHINES[i]}:/root/bin
    ssh -T ${MACHINES[i]} "ln -s /root/bin/Pcrystal /usr/bin/Pcrystal"

    # Copy & run benchmark
    #ssh -T ${MACHINES[i]} "mkdir -p /data/local_tasks/benchmark"
    #scp $TEST_TO_COPY ${MACHINES[i]}:/data/local_tasks/benchmark
    #ssh -T ${MACHINES[i]} "nohup /usr/bin/mpirun -np 8 --allow-run-as-root -wd /data/local_tasks/benchmark /usr/bin/Pcrystal > /data/local_tasks/benchmark/OUTPUT 2>&1 &"
done
