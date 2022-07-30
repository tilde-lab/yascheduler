#!/usr/bin/env bash

MACHINEFILE=$(dirname "$0")/nodes
mapfile -t MACHINES < <(cat "$MACHINEFILE")

SUDOER="admin"
KEY_TOCOPY=~/.ssh/id_rsa.pub

for ((i = 0; i < ${#MACHINES[@]}; i++)); do
	ssh -T -o UserKnownHostsFile=~/.ssh/known_hosts -o StrictHostKeyChecking=no "${MACHINES[i]}" "whoami"
	ssh -T "${MACHINES[i]/root/$SUDOER}" "sudo -- sh -c 'sed -i s/^.*ssh-rsa/ssh-rsa/ /root/.ssh/authorized_keys'"
	ssh-copy-id -f -i $KEY_TOCOPY "${MACHINES[i]}"
done
