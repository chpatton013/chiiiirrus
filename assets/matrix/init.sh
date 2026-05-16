#!/bin/bash
set -euo pipefail

DATA=/data
SERVER_NAME="${SYNAPSE_SERVER_NAME}"
SIGNING_KEY="${DATA}/${SERVER_NAME}.signing.key"

# 1. Signing key (idempotent). Synapse needs this for federation
#    event signing and for E2E key cross-signing.
if [ ! -f "${SIGNING_KEY}" ]; then
  python -m synapse._scripts.generate_signing_key -o "${SIGNING_KEY}"
fi

# 2. One-time random secrets: macaroon (auth tokens), form (CSRF),
#    registration_shared_secret (used by the bot bootstrap CR in a
#    future phase to register the OpenClaw bot account).
for f in macaroon_secret_key form_secret registration_shared_secret; do
  if [ ! -f "${DATA}/${f}" ]; then
    head -c 32 /dev/urandom | base64 | tr -d '\n=' >"${DATA}/${f}"
    chmod 0600 "${DATA}/${f}"
  fi
done
MACAROON_KEY="$(cat "${DATA}/macaroon_secret_key")"
FORM_SECRET="$(cat "${DATA}/form_secret")"
REGISTRATION_SHARED_SECRET="$(cat "${DATA}/registration_shared_secret")"
export MACAROON_KEY FORM_SECRET REGISTRATION_SHARED_SECRET SIGNING_KEY

# 3. Render homeserver.yaml. The template lives at
#    assets/matrix/homeserver.yaml.tmpl in the repo and is shipped
#    here verbatim through the HOMESERVER_YAML_TMPL env var. We
#    expand `${VAR}` references with python3 (always available in
#    the Synapse image). Synapse's own `{{ ... }}` template syntax
#    has no `$` prefix so it passes through unchanged.
python3 -c '
import os, sys
sys.stdout.write(os.path.expandvars(sys.stdin.read()))
' <<<"${HOMESERVER_YAML_TMPL}" >"${DATA}/homeserver.yaml"

# 4. Log config is a static YAML file with no substitutions;
#    write the env-var content straight to disk.
printf '%s' "${LOG_CONFIG_YAML}" >"${DATA}/log.config"

# 5. Application-service registration for openclaw. Same shape as
#    homeserver.yaml: env-var-borne template, expanded with
#    python3 expandvars. APPSERVICE_AS_TOKEN / APPSERVICE_HS_TOKEN
#    come in as ECS secrets so they don't appear in any task-def
#    plaintext. homeserver.yaml.tmpl references this path via
#    app_service_config_files.
python3 -c '
import os, sys
sys.stdout.write(os.path.expandvars(sys.stdin.read()))
' <<<"${APPSERVICE_OPENCLAW_TMPL}" >"${DATA}/appservice-openclaw.yaml"
chmod 0600 "${DATA}/appservice-openclaw.yaml"

echo "matrix-init: homeserver.yaml + appservice-openclaw.yaml rendered for ${SERVER_NAME}"
