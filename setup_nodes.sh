#!/bin/bash
# Pcrystal **Debian 10** installer

MACHINEFILE=$(dirname "$0")/nodes
MACHINES=($( cat $MACHINEFILE ))

BIN_TO_COPY=~/ab_initio/CRYSTAL_bin/debian_10_openmpi_3.1.3/Pcrystal
WWW_TO_COPY=https://tilde.pro/sw/313.tar.gz
WWWMP_TO_COPY=https://tilde.pro/sw/313mp.tar.gz
TEST_TO_COPY=~/ab_initio/universal_test_input/INPUT

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
    ssh -T ${MACHINES[i]} "cd /root/bin && wget $WWW_TO_COPY"
    ssh -T ${MACHINES[i]} "cd /root/bin && tar xvf *.gz"
    #scp $BIN_TO_COPY ${MACHINES[i]}:/root/bin
    #ssh -T ${MACHINES[i]} "ln -s /root/bin/MPPcrystal /usr/bin/Pcrystal"
    ssh -T ${MACHINES[i]} "ln -s /root/bin/Pcrystal /usr/bin/Pcrystal"
    echo "set mouse-=a" > ~/.vimrc

    # Copy & run benchmark
    ssh -T ${MACHINES[i]} "mkdir -p /data/benchmark"
    #scp $TEST_TO_COPY ${MACHINES[i]}:/data/benchmark
    #ssh -T ${MACHINES[i]} "nohup /usr/bin/mpirun -np `grep -c ^processor /proc/cpuinfo` --allow-run-as-root -wd /data/benchmark /usr/bin/Pcrystal > /data/benchmark/OUTPUT 2>&1 &"
    #ssh -T ${MACHINES[i]} "nohup /usr/bin/mpirun -np 24 --allow-run-as-root -wd /data/benchmark /usr/bin/Pcrystal > /data/benchmark/OUTPUT 2>&1 &"
    #ssh -T ${MACHINES[i]} "nohup /usr/bin/mpirun --oversubscribe -np 8 --allow-run-as-root -wd /data/benchmark /usr/bin/Pcrystal > /data/benchmark/OUTPUT 2>&1 &"
    #ssh -T ${MACHINES[i]} "nohup /usr/bin/mpirun -np 4 --allow-run-as-root -wd /data/benchmark /usr/bin/Pcrystal > /data/benchmark/OUTPUT 2>&1 &"
done
