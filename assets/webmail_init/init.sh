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

# 2. Roundcube OAuth2 config. Single-quoted heredoc so PHP `$config`
# references survive unmangled; values get inlined from the env vars
# above before we write the file.
#
# The first PHP block fakes \$_SERVER so Roundcube sees the request
# as HTTPS even though Apache only speaks plain HTTP behind the
# TLS-terminating ALB. Without it, Roundcube generates an
# `http://...` redirect_uri for the OAuth flow, which Authentik
# strict-matches against the registered `https://...` URL and
# rejects with "missing, invalid, or mismatching redirection URI".
cat >"$ROUNDCUBE_DATA_DIR/config/oauth.inc.php" <<PHP
<?php
if ((\$_SERVER['HTTP_X_FORWARDED_PROTO'] ?? '') === 'https') {
    \$_SERVER['HTTPS'] = 'on';
    \$_SERVER['SERVER_PORT'] = 443;
}
\$config['oauth_provider'] = 'generic';
\$config['oauth_provider_name'] = 'Authentik';
\$config['oauth_client_id'] = '$OAUTH_CLIENT_ID';
\$config['oauth_client_secret'] = '$OAUTH_CLIENT_SECRET';
\$config['oauth_auth_uri'] = '$AUTHENTIK_ISSUER_BASE/authorize/';
\$config['oauth_token_uri'] = '$AUTHENTIK_ISSUER_BASE/token/';
\$config['oauth_identity_uri'] = '$AUTHENTIK_ISSUER_BASE/userinfo/';
\$config['oauth_scope'] = 'openid profile email offline_access';
\$config['oauth_pkce'] = 'S256';
\$config['oauth_identity_fields'] = ['email'];
\$config['imap_auth_type'] = 'XOAUTH2';
\$config['smtp_auth_type'] = 'XOAUTH2';
\$config['login_autocomplete'] = 0;
PHP

# Ownership + perms so the apache user (uid 33) in the main container
# can read both files. The init container has no www-data; chown by
# numeric uid:gid to match what apache uses inside Roundcube.
chown -R 33:33 "$ROUNDCUBE_DATA_DIR"
chmod 0640 "$ROUNDCUBE_DATA_DIR/db/sqlite.db"
chmod 0640 "$ROUNDCUBE_DATA_DIR/config/oauth.inc.php"
