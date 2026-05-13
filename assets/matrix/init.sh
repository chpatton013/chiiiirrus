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

# 3. Render homeserver.yaml. Bash interpolates ${...}; Synapse's
#    own template syntax `{{ user.preferred_username }}` passes
#    through as a literal string for Synapse to evaluate at OIDC
#    callback time. DB password and OIDC client_secret are inlined
#    from ECS-injected env vars - Synapse's database.args go
#    straight to psycopg2 which has no `password_path` knob, and
#    the YAML lives on an encrypted EFS access point restricted to
#    uid/gid 991 mode 750, so the exposure is equivalent to the
#    macaroon/form keys already in this file.
cat >"${DATA}/homeserver.yaml" <<EOF
server_name: "${SERVER_NAME}"
public_baseurl: "${PUBLIC_BASEURL}"
pid_file: /data/homeserver.pid

listeners:
  - port: ${SYNAPSE_PORT}
    type: http
    x_forwarded: true
    bind_addresses: ['0.0.0.0']
    resources:
      - names: [client, federation]
        compress: false

database:
  name: psycopg2
  args:
    user: ${DB_USER}
    password: "${DB_PASSWORD}"
    host: ${DB_HOST}
    port: ${DB_PORT}
    database: ${DB_NAME}
    sslmode: require
    cp_min: 5
    cp_max: 10

log_config: /data/log.config
media_store_path: /data/media_store
signing_key_path: ${SIGNING_KEY}

trusted_key_servers:
  - server_name: matrix.org

macaroon_secret_key: "${MACAROON_KEY}"
form_secret: "${FORM_SECRET}"
registration_shared_secret: "${REGISTRATION_SHARED_SECRET}"

enable_registration: false
enable_registration_without_verification: false
serve_server_wellknown: false
report_stats: false
suppress_key_server_warning: true

media_retention:
  remote_media_lifetime: ${REMOTE_MEDIA_LIFETIME}

oidc_providers:
  - idp_id: authentik
    idp_name: Authentik
    issuer: "${OIDC_ISSUER}"
    client_id: "${OIDC_CLIENT_ID}"
    client_secret: "${OIDC_CLIENT_SECRET}"
    scopes: [openid, profile, email]
    user_mapping_provider:
      config:
        localpart_template: "{{ user.preferred_username }}"
        display_name_template: "{{ user.name }}"
        email_template: "{{ user.email }}"
EOF

# 5. Minimal log config so Synapse logs to stdout (CloudWatch picks
#    it up via the awslogs driver).
cat >"${DATA}/log.config" <<'EOF'
version: 1
formatters:
  precise:
    format: '%(asctime)s - %(name)s - %(lineno)d - %(levelname)s - %(request)s - %(message)s'
handlers:
  console:
    class: logging.StreamHandler
    formatter: precise
loggers:
  synapse.storage.SQL:
    level: INFO
root:
  level: INFO
  handlers: [console]
disable_existing_loggers: false
EOF

echo "matrix-init: homeserver.yaml rendered for ${SERVER_NAME}"
