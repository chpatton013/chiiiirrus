#!/bin/bash
# Inline body of `aws ecs execute-command --command "bash -c $(cat
# register_user.sh)"` -- invoked inside the running Synapse Fargate
# task by scripts/matrix/register_user.py.
#
# Receives username as $1 and the admin flag (--admin / --no-admin)
# as $2. Talks to Synapse on http://127.0.0.1:8008 -- the main
# container doesn't have $HOMESERVER_URL in its env (that's only on
# the one-shot bootstrap task), and going out through the public
# ALB would bounce back into this same container anyway. Logs the
# register step to stderr; emits one stdout line:
#   MATRIX_USER_JSON: {"token": "...", "user_id": "...", "device_id": "..."}
set -euo pipefail
USER=$1
ADMIN_FLAG=$2
URL=http://127.0.0.1:8008
PASS=$(head -c 32 /dev/urandom | base64 | tr -d '\n=+/')
python -m synapse._scripts.register_new_matrix_user \
  -c /data/homeserver.yaml \
  -u "$USER" -p "$PASS" "$ADMIN_FLAG" \
  "$URL" >&2
RESP=$(curl -fsS -X POST "$URL/_matrix/client/v3/login" \
  -H 'Content-Type: application/json' \
  -d "{\"type\":\"m.login.password\",\"user\":\"$USER\",\"password\":\"$PASS\",\"initial_device_display_name\":\"$USER\"}")
echo "MATRIX_USER_JSON: $(echo "$RESP" | python3 -c 'import json,sys; r=json.load(sys.stdin); print(json.dumps({"token": r["access_token"], "user_id": r["user_id"], "device_id": r.get("device_id", "")}))')"
