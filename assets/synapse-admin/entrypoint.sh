#!/bin/sh
# Render config.json from the env-templated source. nginx's
# official image runs /docker-entrypoint.d/*.sh in lexical order
# before launching `nginx -g 'daemon off;'` -- by the 40-* slot
# the runtime env (MATRIX_FQDN) is already populated by ECS.
set -eu
envsubst <"/etc/synapse-admin/config.json.tmpl" >"/usr/share/nginx/html/config.json"
