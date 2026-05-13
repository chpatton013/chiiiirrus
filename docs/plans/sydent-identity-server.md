# Self-host a Matrix identity server (sydent)

## Context

Element's "Identity server" setting currently points at
`vector.im`, the public identity server run by element-hq. An
identity server is the Matrix-protocol-specific service that maps
third-party identifiers (email, phone) to MXIDs:

- "Invite by email" looks the email up in the identity server's
  table.
- "Find friends by phone number" same idea.
- The "discoverable by other users" toggle in Element controls
  whether your account's email + phone are published into that
  table.

For a single-household deployment this is mostly vestigial. The
operator and their partner know each other's MXIDs directly, will
not be inviting people by email, and don't want their email +
phone published on a third-party server's bulletin board.
**The currently-correct answer is: skip this entirely. Don't
self-host, don't point Element at any identity server.**

This plan exists so that if the calculation ever changes (more
users joining, federation with friends, etc.), the steps are
recorded.

## Scope

The reference implementation is
[sydent](https://github.com/element-hq/sydent) (Python, sqlite or
postgres backend, currently in maintenance mode at element-hq).

Minimum useful deployment:

- One Fargate task running sydent at `id.<public_domain>` (or
  `vector.<public_domain>` to match the legacy naming the operator
  saw in Element's UI).
- Email-only support (skip phone — phone validation needs a
  Twilio/Signalwire integration and is high-friction for ~zero
  gain at our scale).
- SMTP delegation to the MailStack so sydent's "verify this email"
  emails come from `<public_domain>` rather than a third-party
  sender.
- Synapse `account_threepid_delegates` pointing at our sydent so
  email validation during signup/login flows through us instead
  of `vector.im`.

## Building blocks

### Sydent config

Sydent reads `/data/sydent/sydent.conf`. Minimal fields:

```ini
[general]
server_name = id.<public_domain>
templates.path = /sydent/res

[db]
db.file = /data/sydent/sydent.db

[http]
clientapi.http.bind_address = 0.0.0.0
clientapi.http.port = 8090
federation.verifycerts = true

[email]
email.from = identity@<public_domain>
email.smtphost = smtp.<public_domain>
email.smtpport = 587
email.smtpusername = ...
email.smtppassword = ...
```

### CDK shape

- `infra/stacks/sydent_stack.py` — modeled after VaultwardenStack:
  - SharedEfsVolume for `/data/sydent` (sqlite + signing keys).
  - Single Fargate task pulling
    `matrixdotorg/sydent` or a custom-built image.
  - PublicHttpAlb at `id.<public_domain>` terminating TLS,
    forwarding to port 8090.
  - StandardBackupPlan on the EFS.
- `infra/models/sydent_config.py` — subdomain, image version,
  email-from address.
- `config.toml` — new `[sydent]` block.
- Synapse change: append to `homeserver.yaml` (the init.sh
  template):

  ```yaml
  account_threepid_delegates:
    email: https://id.<public_domain>
  ```

- SES relay for outbound mail: sydent talks to MailStack on
  port 587 via the VPC mynetworks path (no SASL required, same
  pattern Authentik already uses).

### Signing keys

Sydent has its own ed25519 signing key for the identity
service's federation responses. Generated on first start;
persists in `/data/sydent/sydent.db`. RETAIN the EFS so the key
survives across instance recreates (matches the pattern for
every other piece of identity material in the stack).

## Open decisions

- **Subdomain.** `id.<public_domain>` (concise, matches Matrix
  spec terminology) vs. `vector.<public_domain>` (matches what
  Element's UI calls the legacy default). Pick one.
- **Phone support.** Default no. Revisit only if there's a real
  use case (e.g., inviting non-tech-savvy family members who
  identify each other by phone).
- **Discovery list opt-in.** Sydent has a "discover by email"
  toggle in client UIs. For our use case, default to off; users
  can opt in if they want.

## Verification

- After deploy, `curl https://id.<public_domain>/_matrix/identity/v2`
  returns the sydent version banner.
- Send a test "invite by email" from Element to a known address
  on our domain; sydent receives the lookup; the receiver gets
  the invite email via our SES relay; clicking the invite
  redirects to Element with the room pre-joined.
- Synapse's `_matrix/client/r0/account/3pid/email/requestToken`
  flow goes through sydent (not vector.im); the validation email
  hits our MX, not Element's.

## Why this is "skip for now"

For a one-MXID-each-other household:
- No email lookups happen (we don't invite people by email).
- No phone lookups happen.
- The "discoverable" toggle has nothing to discover.
- vector.im is harmless: it's read-only for our flows since we
  don't publish anything to it.

Sydent operates correctly as a *missing* service for our setup.
This plan moves from "useful infrastructure" to "useful
infrastructure" only if the deployment grows users who want to
find each other by external identifiers.
