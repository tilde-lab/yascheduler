#!/bin/bash
# Remote control helper

MACHINEFILE=$(dirname $0)/../nodes
MACHINES=($( cat $MACHINEFILE ))

for (( i=0; i<${#MACHINES[@]}; i++ )); do
    echo "Running $@ at "${MACHINES[i]}
    ssh -T ${MACHINES[i]} "$@"
done
