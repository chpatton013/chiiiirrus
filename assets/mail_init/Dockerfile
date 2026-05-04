# Init container for the docker-mailserver Fargate task.
#
# Bundles aws-cli + lego (Go binary, single static executable) onto Alpine.
# At task start the container's command (set in MailStack) writes the
# DKIM key + postfix-accounts.cf + postfix-main.cf onto the EFS config
# volume and runs `lego` to issue/renew the Let's Encrypt cert via
# Route53 DNS-01.
#
# Pinned versions so init startup is deterministic across deploys.

FROM alpine:3.19

ARG LEGO_VERSION=4.16.1

RUN apk add --no-cache aws-cli ca-certificates curl jq openssl tar \
    && mkdir -p /usr/local/bin \
    && curl -fsSL "https://github.com/go-acme/lego/releases/download/v${LEGO_VERSION}/lego_v${LEGO_VERSION}_linux_amd64.tar.gz" \
       | tar -xzC /usr/local/bin lego \
    && chmod +x /usr/local/bin/lego

# No ENTRYPOINT or CMD - the ECS task definition supplies the command
# via container_kwargs entry_point + command, mirroring the
# headscale_api_key pattern (it lets us run init logic that's specific
# to this stack without baking it into the image).
