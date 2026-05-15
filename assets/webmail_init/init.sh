#!/bin/sh
#
# Init container for WebmailStack (Roundcube Fargate task).
#
# Idempotent. Runs on every task start before the main Roundcube
# container; the main container has a SUCCESS dependency on this one.
#
# Three responsibilities:
#
#   1. Bootstrap the sqlite DB schema. The upstream Roundcube image's
#      entrypoint runs schema init only when sqlite.db is absent; on
#      a fresh EFS access point an empty file gets touched and the
#      entrypoint skips, leaving Roundcube to crash on every request
#      with `no such table: session`. We re-create the DB if it's
#      absent or has no `session` table.
#
#   2. Template /var/roundcube/config/oauth.inc.php so Roundcube can
#      run the OAuth2 flow against Authentik (replacing the IMAP-
#      password login with end-to-end OAUTHBEARER).
#
#   3. Drop a sentinel file at /var/roundcube/config/include.php that
#      the main container's entrypoint hook (see below) sources, so
#      our oauth config gets loaded by Roundcube even though
#      Roundcube itself only reads /var/www/html/config/.
#
# Required env (set by infra/stacks/webmail_stack.py on the init container):
#   ROUNDCUBE_DATA_DIR    - EFS mount path inside the container
#                           (e.g. /var/roundcube)
#   AUTHENTIK_ISSUER_BASE - e.g. https://auth.<domain>/application/o
#
# Required ECS-injected secrets:
#   OAUTH_CLIENT_ID       - from authentik/oidc/roundcube .client_id
#   OAUTH_CLIENT_SECRET   - from authentik/oidc/roundcube .client_secret

set -eu

mkdir -p "$ROUNDCUBE_DATA_DIR/db" "$ROUNDCUBE_DATA_DIR/config"

# 1. Sqlite schema bootstrap. If the file is missing or has no
# `session` table, drop it and reload from the schema we baked into
# the image at build time.
db="$ROUNDCUBE_DATA_DIR/db/sqlite.db"
query="SELECT 1 FROM sqlite_master WHERE type='table' AND name='session' LIMIT 1"
if [ ! -f "$db" ] || ! sqlite3 "$db" "$query" | grep -q 1; then
  rm -f "$db"
  sqlite3 "$db" </usr/local/share/roundcube/sqlite.initial.sql
  echo "schema loaded"
else
  echo "schema present"
fi

# 2. Roundcube OAuth2 config. The template lives at
# /usr/local/share/roundcube/oauth.inc.php.tmpl (baked into the
# image alongside the SQL schema). envsubst with an explicit
# allowlist substitutes only the three env vars we care about;
# PHP's own `$config[...]` and `$_SERVER[...]` references stay
# intact because they're not in the list.
# shellcheck disable=SC2016 # envsubst's allowlist syntax needs literal ${...}
envsubst '${OAUTH_CLIENT_ID} ${OAUTH_CLIENT_SECRET} ${AUTHENTIK_ISSUER_BASE}' \
  </usr/local/share/roundcube/oauth.inc.php.tmpl \
  >"$ROUNDCUBE_DATA_DIR/config/oauth.inc.php"

# Ownership + perms so the apache user (uid 33) in the main container
# can read both files. The init container has no www-data; chown by
# numeric uid:gid to match what apache uses inside Roundcube.
chown -R 33:33 "$ROUNDCUBE_DATA_DIR"
chmod 0640 "$ROUNDCUBE_DATA_DIR/db/sqlite.db"
chmod 0640 "$ROUNDCUBE_DATA_DIR/config/oauth.inc.php"
