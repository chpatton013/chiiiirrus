# Init container for the Roundcube Fargate task.
#
# Alpine + sqlite3 CLI + the Roundcube schema baked at build time.
# We deliberately don't FROM roundcube/roundcubemail here: the only
# upstream artifact we need is SQL/sqlite.initial.sql, and pulling the
# whole image from Docker Hub at synth time blew through the
# unauthenticated rate limit on multi-deploy days. Curling the single
# SQL file from GitHub instead is independently rate-limited and
# version-locked via the ROUNDCUBE_SOURCE_VERSION build arg (the bare
# source tag without the docker image's "-apache" / "-fpm" suffix).

FROM alpine:3.19

ARG ROUNDCUBE_SOURCE_VERSION=1.6.10

RUN apk add --no-cache ca-certificates curl gettext jq sqlite \
    && mkdir -p /usr/local/share/roundcube \
    && curl -fsSL \
       "https://raw.githubusercontent.com/roundcube/roundcubemail/${ROUNDCUBE_SOURCE_VERSION}/SQL/sqlite.initial.sql" \
       -o /usr/local/share/roundcube/sqlite.initial.sql

COPY init.sh /usr/local/bin/init.sh
COPY oauth.inc.php.tmpl /usr/local/share/roundcube/oauth.inc.php.tmpl
RUN chmod +x /usr/local/bin/init.sh

ENTRYPOINT []
CMD ["/usr/local/bin/init.sh"]
