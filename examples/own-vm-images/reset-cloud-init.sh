#!/usr/bin/env bash
# reset cloud init state

cloud-init clean --logs --machine-id --seed

rm -rf /run/cloud-init/*
rm -rf /var/lib/cloud/*
