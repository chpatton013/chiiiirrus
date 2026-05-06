# Init container for the Roundcube Fargate task.
#
# Layered on the Roundcube image so init.sh can talk to sqlite via PHP
# and read /var/www/html/SQL/sqlite.initial.sql for first-boot schema
# load. ROUNDCUBE_VERSION is wired through from config.toml's
# [webmail].image_version via the WebmailStack DockerImageAsset
# build_args, so the init's PHP/SQL stays version-locked to the main
# container's PHP/SQL.

ARG ROUNDCUBE_VERSION=1.6.10-apache
FROM roundcube/roundcubemail:${ROUNDCUBE_VERSION}

COPY init.sh /usr/local/bin/init.sh
RUN chmod +x /usr/local/bin/init.sh

ENTRYPOINT []
CMD ["/usr/local/bin/init.sh"]
