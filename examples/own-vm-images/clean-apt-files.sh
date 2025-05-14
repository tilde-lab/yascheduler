#!/usr/bin/env bash
# remove apt files

export DEBIAN_FRONTEND=noninteractive

apt-get -y autopurge
apt-get -y clean

rm -rf /var/lib/apt/lists/*
