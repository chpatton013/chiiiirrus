#!/bin/sh
#
# Init container for MailStack (docker-mailserver Fargate task).
#
# Idempotent. Runs on every task start before the main container; the
# main container has a SUCCESS dependency on this one. Reads its
# configuration entirely from env vars set on the container and
# from Secrets Manager via secret IDs.
#
# Required env (set by infra/stacks/mail_stack.py on the init container):
#   CONFIG_MOUNT         - EFS mount path inside the container
#                          (e.g. /tmp/docker-mailserver)
#   LE_DIR               - Let's Encrypt cert dir under CONFIG_MOUNT
#   DKIM_SELECTOR        - DKIM key selector (e.g. "s1")
#   RSPAMD_UI_PORT       - rspamd web UI bind port (e.g. 11334)
#   POSTMASTER_ADDRESS   - postmaster mailbox (postmaster@<domain>)
#   MAIL_FQDN            - mail server FQDN (e.g. smtp.<domain>)
#   MAIL_DOMAIN          - public domain for user mailboxes
#   MAIL_USERS           - space-separated list of user localparts;
#                          empty means postmaster-only
#   VPC_CIDR             - VPC CIDR (e.g. 10.0.0.0/16); used for
#                          Postfix mynetworks + rspamd secure_ip
#   DKIM_SECRET          - Secrets Manager ID for the DKIM private key
#   POSTMASTER_SECRET    - Secrets Manager ID for the postmaster password
#   AUTHENTIK_ISSUER_BASE - e.g. https://auth.<domain>/application/o ;
#                          init pulls JWKS from <base>/roundcube/jwks/
#                          for Dovecot's OAUTHBEARER passdb

set -eu

mkdir -p \
  "$CONFIG_MOUNT/rspamd/dkim" \
  "$CONFIG_MOUNT/rspamd/override.d" \
  "$CONFIG_MOUNT/jwks" \
  "$LEGO_PATH"

# 1. DKIM private key, written at the rspamd-expected path. The DKIM
# Custom Resource Lambda owns the keypair lifecycle; we just copy the
# private side onto EFS so rspamd can sign on outbound.
aws secretsmanager get-secret-value \
  --secret-id "$DKIM_SECRET" \
  --query SecretString --output text |
  jq -r .secret \
    >"$CONFIG_MOUNT/rspamd/dkim/$DKIM_SELECTOR.key"
chmod 0600 "$CONFIG_MOUNT/rspamd/dkim/$DKIM_SELECTOR.key"

# 2. postfix-accounts.cf - postmaster + per-user mailboxes. One row
# per mailbox: <full-address>|{SHA512-CRYPT}<dovecot hash>.
{
  pm=$(aws secretsmanager get-secret-value \
    --secret-id "$POSTMASTER_SECRET" \
    --query SecretString --output text | jq -r .secret)
  echo "$POSTMASTER_ADDRESS|{SHA512-CRYPT}$(openssl passwd -6 "$pm")"
  for user in $MAIL_USERS; do
    pw=$(aws secretsmanager get-secret-value \
      --secret-id "mail/users/$user" \
      --query SecretString --output text | jq -r .secret)
    echo "$user@$MAIL_DOMAIN|{SHA512-CRYPT}$(openssl passwd -6 "$pw")"
  done
} >"$CONFIG_MOUNT/postfix-accounts.cf"

# 3a. mynetworks override so VPC traffic submits without SASL.
cat >"$CONFIG_MOUNT/postfix-main.cf" <<EOF
mynetworks = 127.0.0.1/32 [::1]/128 $VPC_CIDR
EOF

# 3b. Submission service (587) override: re-add permit_mynetworks to
# all three restriction phases. Default DMS submission is
# `permit_sasl_authenticated,reject` which would otherwise reject
# in-VPC clients (Authentik, Vaultwarden) that don't speak SASL.
cat >"$CONFIG_MOUNT/postfix-master.cf" <<'EOF'
submission/inet/smtpd_client_restrictions=permit_mynetworks,permit_sasl_authenticated,reject
submission/inet/smtpd_relay_restrictions=permit_mynetworks,permit_sasl_authenticated,reject
submission/inet/smtpd_recipient_restrictions=permit_mynetworks,permit_sasl_authenticated,reject
EOF

# 3c. rspamd worker-controller override: bind the HTTP UI to
# 0.0.0.0:$RSPAMD_UI_PORT (default is 127.0.0.1) and skip rspamd's
# built-in password auth for VPC traffic. The internal ALB's OIDC
# action is the gate.
cat >"$CONFIG_MOUNT/rspamd/override.d/worker-controller.inc" <<EOF
bind_socket = "*:$RSPAMD_UI_PORT";
secure_ip = "$VPC_CIDR";
EOF

# 3d. Dovecot OAUTHBEARER passdb. Lets Roundcube authenticate IMAP
# and SMTP-submission with an Authentik-issued access token, no
# password ever templated. Additive to the existing passwd-file
# passdb so mutt / Apple Mail keep working with passwords.
#
# JWKS is fetched on every init run (cheap) so signing-key rotation
# at Authentik propagates without manual intervention.
curl -sf "$AUTHENTIK_ISSUER_BASE/roundcube/jwks/" \
  >"$CONFIG_MOUNT/jwks/keys.json"

# Single-quoted heredoc - `$auth_mechanisms` is a Dovecot variable,
# not a shell var, and must reach the file unmangled.
cat >"$CONFIG_MOUNT/dovecot.cf" <<'EOF'
auth_mechanisms = $auth_mechanisms oauthbearer xoauth2
passdb {
  driver = oauth2
  mechanisms = oauthbearer xoauth2
  args = /tmp/docker-mailserver/dovecot-oauth2.conf.ext
}
EOF

# active_attribute and active_value must both be empty for local-only
# JWT validation; Dovecot rejects the config if one is set without
# the other. %Lu = lowercase full username (Dovecot format spec).
cat >"$CONFIG_MOUNT/dovecot-oauth2.conf.ext" <<EOF
local_validation_key_dict = fs:posix:prefix=$CONFIG_MOUNT/jwks/
introspection_mode = local
issuers = $AUTHENTIK_ISSUER_BASE/roundcube/
username_attribute = email
username_format = %Lu
active_attribute =
active_value =
EOF

# 4. Let's Encrypt cert. Issue on first deploy, renew if <30 days
# until expiry. State persists on EFS via $LEGO_PATH so subsequent
# init runs are idempotent.
if [ ! -f "$LEGO_PATH/certificates/$MAIL_FQDN.crt" ]; then
  lego --path="$LEGO_PATH" --email="$POSTMASTER_ADDRESS" \
    --domains="$MAIL_FQDN" --dns=route53 --accept-tos run
else
  lego --path="$LEGO_PATH" --email="$POSTMASTER_ADDRESS" \
    --domains="$MAIL_FQDN" --dns=route53 renew --days=30 || true
fi
