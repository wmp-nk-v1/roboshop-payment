#!/bin/sh
set -a
[ -f /data/params ] && . /data/params
set +a
exec "$@"
