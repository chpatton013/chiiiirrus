# Init container for the docker-mailserver Fargate task.
#
# Bundles aws-cli + lego (Go binary, single static executable) onto
# Alpine, plus the init.sh script. At task start ECS invokes init.sh,
# which reads its config entirely from container env + Secrets
# Manager, writes Postfix / Dovecot / rspamd overrides onto the EFS
# config volume, fetches Authentik's JWKS, and issues/renews the
# Let's Encrypt cert via Route53 DNS-01.
#
# Pinned versions so init startup is deterministic across deploys.

FROM alpine:3.19

ARG LEGO_VERSION=4.16.1

RUN apk add --no-cache aws-cli ca-certificates curl jq openssl tar \
    && mkdir -p /usr/local/bin \
    && curl -fsSL "https://github.com/go-acme/lego/releases/download/v${LEGO_VERSION}/lego_v${LEGO_VERSION}_linux_amd64.tar.gz" \
       | tar -xzC /usr/local/bin lego \
    && chmod +x /usr/local/bin/lego

COPY init.sh /usr/local/bin/init.sh
RUN chmod +x /usr/local/bin/init.sh

# ENTRYPOINT/CMD intentionally not set - the ECS task definition
# invokes /usr/local/bin/init.sh via entry_point in MailStack so the
# shell stays a thin wrapper around the script.
