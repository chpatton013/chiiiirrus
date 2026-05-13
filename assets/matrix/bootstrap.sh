#!/bin/bash
set -euo pipefail
BOT_USERNAME=openclaw-bot
BOT_PASSWORD="$(head -c 32 /dev/urandom | base64 | tr -d '\n=+/')"

python -m synapse._scripts.register_new_matrix_user \
  -c /data/homeserver.yaml \
  -u "${BOT_USERNAME}" \
  -p "${BOT_PASSWORD}" \
  --no-admin \
  "${HOMESERVER_URL}" >&2

curl -fsS -X POST "${HOMESERVER_URL}/_matrix/client/v3/login" \
  -H "Content-Type: application/json" \
  -d "{\"type\":\"m.login.password\",\"user\":\"${BOT_USERNAME}\",\"password\":\"${BOT_PASSWORD}\",\"initial_device_display_name\":\"openclaw-bot\"}" |
  python3 -c "
import json, sys
r = json.load(sys.stdin)
print(json.dumps({'token': r['access_token'], 'user_id': r['user_id'], 'device_id': r.get('device_id', '')}))
"
