#!/bin/sh
# Writes /etc/headplane/config.yaml for the headplane main container.
# Headplane v0.6+ refuses to start without it. Run by SharedVolumeInit
# (aws-cli image, busybox-ash /bin/sh). Inputs (env):
#   HP_COOKIE_NAME    Secrets Manager id, JSON {"secret": <b64-or-raw>}
#   HP_APIKEY_NAME    Secrets Manager id, JSON {"secret": <api-key>}
#   HP_OIDC_NAME      Secrets Manager id, JSON {"client_id", "client_secret"}
#   HP_HEADSCALE_URL  http://<headscale>:<port>
#   HP_OIDC_ISSUER    Authentik issuer URL
#   HP_CONFIG_INIT_PY  Python source for the config-emitting script
#                      (injected by HeadscaleStack via env so the
#                      script lives in the repo as a sibling .py
#                      file for pyright/black coverage).
set -eu

COOKIE="$(aws secretsmanager get-secret-value --secret-id "${HP_COOKIE_NAME}" --query SecretString --output text | jq -r .secret)"
APIKEY="$(aws secretsmanager get-secret-value --secret-id "${HP_APIKEY_NAME}" --query SecretString --output text | jq -r .secret)"
OIDC="$(aws secretsmanager get-secret-value --secret-id "${HP_OIDC_NAME}" --query SecretString --output text)"
CLIENT_ID="$(echo "$OIDC" | jq -r .client_id)"
CLIENT_SECRET="$(echo "$OIDC" | jq -r .client_secret)"
export COOKIE APIKEY CLIENT_ID CLIENT_SECRET

python3 -c "$HP_CONFIG_INIT_PY"
