#!/bin/sh
# Writes /etc/headplane/config.yaml for the headplane main container.
# Headplane v0.6+ refuses to start without it. Run by SharedVolumeInit
# (aws-cli image, busybox-ash /bin/sh). Inputs (env):
#   HP_COOKIE_NAME    Secrets Manager id, JSON {"secret": <b64-or-raw>}
#   HP_APIKEY_NAME    Secrets Manager id, JSON {"secret": <api-key>}
#   HP_OIDC_NAME      Secrets Manager id, JSON {"client_id", "client_secret"}
#   HP_HEADSCALE_URL  http://<headscale>:<port>
#   HP_OIDC_ISSUER    Authentik issuer URL
set -eu

COOKIE="$(aws secretsmanager get-secret-value --secret-id "${HP_COOKIE_NAME}" --query SecretString --output text | jq -r .secret)"
APIKEY="$(aws secretsmanager get-secret-value --secret-id "${HP_APIKEY_NAME}" --query SecretString --output text | jq -r .secret)"
OIDC="$(aws secretsmanager get-secret-value --secret-id "${HP_OIDC_NAME}" --query SecretString --output text)"
CLIENT_ID="$(echo "$OIDC" | jq -r .client_id)"
CLIENT_SECRET="$(echo "$OIDC" | jq -r .client_secret)"
export COOKIE APIKEY CLIENT_ID CLIENT_SECRET

# Heredoc with quoted 'PYEOF' prevents shell expansion inside the
# Python code. The config is built as a dict then dumped as JSON
# (headplane accepts JSON wherever it accepts YAML).
python3 <<'PYEOF'
import json, os
cfg = {
    'server': {'host': '0.0.0.0', 'port': 3000,
               'cookie_secret': os.environ['COOKIE'], 'cookie_secure': False, 'data_path': '/tmp/headplane/'},
    'headscale': {'url': os.environ['HP_HEADSCALE_URL'], 'config_strict': False},
    'oidc': {
        'issuer': os.environ['HP_OIDC_ISSUER'],
        'client_id': os.environ['CLIENT_ID'],
        'client_secret': os.environ['CLIENT_SECRET'],
        'token_endpoint_auth_method': 'client_secret_basic',
        'disable_api_key_login': False,
        'headscale_api_key': os.environ['APIKEY'],
    },
}
open('/etc/headplane/config.yaml', 'w').write(json.dumps(cfg, indent=2))
print('headplane config.yaml written')
PYEOF
