#!/usr/bin/env bash
# must discard the now unused blocks from the disk

dd if=/dev/zero of=/zero bs=4M || true
sync
rm -f /zero
