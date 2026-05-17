FROM nginx:1.27-alpine

# Synapse-admin (now branded "Ketesa" upstream) is a static React
# SPA. Fetch the release tarball at image build time and unpack
# into nginx's docroot. `--strip-components=1` drops the top-level
# `ketesa/` directory in the tarball.
ARG SYNAPSE_ADMIN_VERSION=v1.2.1
RUN apk add --no-cache curl gettext \
 && rm -rf /usr/share/nginx/html/* \
 && curl -fsSL "https://github.com/etkecc/ketesa/releases/download/${SYNAPSE_ADMIN_VERSION}/ketesa.tar.gz" \
    | tar -xz -C /usr/share/nginx/html --strip-components=1 \
 && rm /usr/share/nginx/html/config.json

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY config.json.tmpl /etc/synapse-admin/config.json.tmpl
COPY entrypoint.sh /docker-entrypoint.d/40-render-config.sh
RUN chmod +x /docker-entrypoint.d/40-render-config.sh

EXPOSE 8080
