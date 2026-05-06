#!/bin/sh
#
# Init container for WebmailStack (Roundcube Fargate task).
#
# Idempotent. Runs on every task start before the main Roundcube
# container; the main container has a SUCCESS dependency on this one.
#
# Two responsibilities:
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

# 1. Sqlite schema bootstrap. PHP can talk to sqlite via PDO so we
# don't need the sqlite3 CLI in this image. shellcheck doesn't know
# the body is PHP, not shell, so silence its $-expansion warning.
# shellcheck disable=SC2016
php -r '
  $f = getenv("ROUNDCUBE_DATA_DIR") . "/db/sqlite.db";
  $need_init = !file_exists($f);
  if (!$need_init) {
    try {
      $db = new PDO("sqlite:" . $f);
      $r = $db->query(
        "SELECT name FROM sqlite_master WHERE type=\"table\" AND name=\"session\""
      )->fetch();
      $need_init = !$r;
    } catch (Throwable $e) {
      $need_init = true;
    }
  }
  if ($need_init) {
    @unlink($f);
    $db = new PDO("sqlite:" . $f);
    $db->exec(
      file_get_contents("/var/www/html/SQL/sqlite.initial.sql")
    );
    echo "schema loaded\n";
  } else {
    echo "schema present\n";
  }
'

# 2. Roundcube OAuth2 config. Single-quoted heredoc so PHP `$config`
# references survive unmangled; values get inlined from the env vars
# above before we write the file.
cat >"$ROUNDCUBE_DATA_DIR/config/oauth.inc.php" <<PHP
<?php
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

# Ownership + perms so the apache user can read both files.
chown -R www-data:www-data "$ROUNDCUBE_DATA_DIR"
chmod 0640 "$ROUNDCUBE_DATA_DIR/db/sqlite.db"
chmod 0640 "$ROUNDCUBE_DATA_DIR/config/oauth.inc.php"
