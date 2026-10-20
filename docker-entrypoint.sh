#!/bin/sh
set -eu
python /app/brane.py setup --skip-install --config /data/starter.json --business "${BRAIN_BUSINESS_ID:-personal}"
exec python /app/scripts/starter.py serve --config /data/starter.json --host 0.0.0.0 --port 7778
