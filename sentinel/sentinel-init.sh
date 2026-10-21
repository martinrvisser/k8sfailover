#!/bin/sh
set -eu

# Copy the sentinel config to a writable location since
# sentinel rewrites its config file on failover events.
cp /etc/sentinel/sentinel.conf /tmp/sentinel.conf

# Replace the static primary hostname with the actual current primary,
# discovered by querying the other pods. On first boot of valkey-0 it
# will be the primary; on subsequent starts we trust the saved config.
echo "Starting sentinel with config:"
cat /tmp/sentinel.conf

exec valkey-server /tmp/sentinel.conf --sentinel
