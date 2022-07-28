#!/bin/bash
# Remote control helper

MACHINEFILE=$(dirname "$0")/nodes
mapfile -t MACHINES < <(cat "$MACHINEFILE")

for ((i = 0; i < ${#MACHINES[@]}; i++)); do
	echo "Running $* at ${MACHINES[i]}"
	ssh -T "${MACHINES[i]}" "$@"
	#ssh -i /home/Evgeny/data/ya/yakey-rndslzhc -T "${MACHINES[i]}" "$@"
	#ssh-copy-id -f -i id_rsa.pub "${MACHINES[i]}"
	#yasetnode ${MACHINES[i]} --remove-soft
	printf "\r\n"
done
