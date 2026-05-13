# Self-host Element-Web

## Context

Today the operator's only Matrix client is Element-Web at
`app.element.io` (or Element Desktop, which is the same bundle wrapped).
That's a third-party trust dependency: Element's hosted JS is downloaded
fresh every time, and any compromise of `app.element.io` would land
arbitrary code in the browser session that holds the operator's
recovery key + cross-signing private keys + room history.

Element-Web is just a React SPA. Self-hosting it is mechanical:
fetch a pinned release tarball, serve the static files from S3 +
CloudFront, point its `config.json` at our own Synapse, and stop
trusting Element's hosted version.

This isn't urgent (the hosted version works fine), but it's
cheap, removes a real third-party trust edge, and unblocks
pinning the Element version to a known-good one if a future
release introduces a regression.

## Scope

- New `ElementWebStack` serving `element.<public_domain>` (or
  `chat.<public_domain>` — see open decisions).
- HTTPS via ACM cert, fronted by CloudFront with default
  behavior serving Element's static files from a private S3 bucket
  (Origin Access Control, no public-read on the bucket).
- `config.json` pre-baked with `default_server_config` pointing at
  `matrix.<public_domain>` so users get our homeserver out of the
  box; the "Edit" homeserver dialog stays available for power users.
- No Authentik gate. Element drives its own OIDC flow against
  Synapse, which is already wired to Authentik. Putting Authentik
  in front of Element-Web would double-prompt the user.

## Building blocks

### Element release fetching

Two options:

1. **Build-time download.** The CDK asset is a script that runs at
   synth time, downloads `element-v<version>.tar.gz` from GitHub
   releases, unpacks, and produces a directory CDK uploads to S3.
   Verifies the published `.asc` signature against the Element
   release key.
2. **Pre-bundled.** Check in `assets/element-web/dist/` at the
   pinned version, commit-bump on each version refresh.

Option 1 keeps the repo small + makes version bumps a one-line
config change. Option 2 is fully reproducible but checks in
~100 MB of compiled JS. **Recommend option 1.**

### config.json template

Element reads `/config.json` at load time. Critical fields:

```jsonc
{
  "default_server_config": {
    "m.homeserver": {
      "base_url": "https://matrix.<public_domain>",
      "server_name": "<public_domain>"
    }
  },
  "brand": "Element",
  "show_labs_settings": false,
  // Disable third-party integrations so the client doesn't
  // phone home to scalar.vector.im / element.io services.
  "integrations_ui_url": "",
  "integrations_rest_url": "",
  "integrations_widgets_urls": [],
  "default_country_code": "US",
  "disable_custom_urls": false,
  "disable_guests": true
}
```

Template lives under `assets/element-web/config.json.tmpl`,
substituted at CDK synth time with the homeserver FQDN.

### CDK shape

- `infra/stacks/element_web_stack.py` — modeled on the now-
  ApexEdgeStack pattern: cert + distribution + S3 bucket +
  Route53 record. Pinned to `us-east-1` for the CloudFront cert.
- `infra/models/element_web_config.py` — `subdomain`, `version`
  (Element release tag), optional `branding` overrides.
- `config.toml` — new `[element_web]` block.
- `app_builder.py` — instantiate, no behaviors/content
  contributions to ApexEdgeStack (Element-Web lives on its own
  subdomain).
- `assets/element-web/` — `config.json.tmpl`, optional `fetch.sh`
  if going the build-time-download route.

## Open decisions

- **Subdomain.** `element.<domain>` is the obvious name but
  reinforces the Element-brand framing. `chat.<domain>` is
  generic. `web.<domain>` is shorter. Pick one and commit.
- **Build-time download vs. checked-in dist.** See above.
- **Auto-update mechanism.** Element releases land weekly. The
  pin-bump becomes a manual cadence; this is fine for a personal
  deployment.
- **Self-hosted Element call backend?** Element-Call is a
  separate service for voice/video rooms; if we want
  voice/video in our Matrix rooms, we'd also need a TURN server
  and the call backend. Probably out of scope for v1.

## Verification

- `bin/cdk synth ElementWebStack` produces a template that
  references the asset.
- After deploy, `https://element.<public_domain>` loads the
  client; `/config.json` shows the homeserver pointing at
  `matrix.<public_domain>`.
- Sign in via SSO (Authentik) round-trips correctly; reload
  Element and confirm session persists.
- Inspect Network tab: no requests to `scalar.vector.im`,
  `element.io`, `vector.im` (third-party integration disabling
  worked).

## Out of scope

- Element Desktop self-host. Element Desktop is just a wrapper
  around the same static bundle; the user can install the
  official build and `Custom server` it to our homeserver.
- Element-Call (voice/video). Separate service, separate plan
  if/when we want it.
- Mobile (Element-iOS / Element-Android). Distributed via the
  app stores; can't self-host. Configure manually with the
  homeserver URL.
