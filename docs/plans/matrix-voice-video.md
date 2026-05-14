# Matrix voice/video: TURN + Element-Call

## Context

Voice/video on Matrix splits into two independent pieces and we
need both for full functionality:

1. **TURN** — the WebRTC NAT-traversal relay. Without one, two
   peers behind symmetric NAT (most consumer routers, all mobile
   carriers) cannot establish a media path. With one, Synapse
   hands clients ephemeral TURN credentials and the relay carries
   the media when direct connectivity fails. TURN by itself is
   enough for **1:1 calls** via Element's built-in WebRTC caller.
2. **Element-Call** — Matrix's modern multi-party voice/video
   service. It's a separate web app + LiveKit SFU + a Matrix-aware
   JWT authenticator. Required for **group calls** of any size,
   and for the new "ringing call" UI in Element-Web. Group calls
   in legacy Element used a full mesh (every participant connects
   to every other) which falls over above ~4 participants;
   Element-Call's LiveKit SFU forwards a single uplink to N
   downlinks.

The two pieces are independent in deployment order. TURN alone
unlocks 1:1 calls today; Element-Call layers on top later and
also uses the TURN server for its own NAT traversal.

Today neither exists in this deployment. Clients can text-chat
and join rooms but every voice/video call attempt fails as soon
as one participant is behind real-world NAT — i.e. always.

## Scope

**Phase 1 — TURN server (coturn).**
- One `coturn` instance reachable at `turn.<public_domain>`,
  with a static Elastic IP.
- Synapse `turn_uris` / `turn_shared_secret` configured so it
  hands ephemeral credentials to clients.
- ACME-issued TLS cert for `turns:` (TLS-TURN) so corporate
  firewalls that allow 443/TCP can still relay.
- StandardBackupPlan or equivalent for the cert/private key
  material.

**Phase 2 — Element-Call.**
- LiveKit SFU running in our VPC.
- `lk-jwt-service` (the Matrix-aware JWT minter) on an ALB at
  `lk-jwt.<public_domain>`.
- Static Element-Call SPA at `call.<public_domain>` (same
  S3 + CloudFront pattern as `ElementWebStack`).
- Synapse `experimental_features` toggles enabled so Element-Web
  surfaces the Call UI; well-known points clients at the Element-
  Call frontend.

## Building blocks — Phase 1 (TURN)

### coturn host

coturn is the standard open-source TURN server. Configuration is
a single `turnserver.conf` file. The interesting fields:

```
realm=<public_domain>
server-name=turn.<public_domain>
listening-port=3478
tls-listening-port=5349
external-ip=<elastic-ip>
min-port=49152
max-port=49251
fingerprint
lt-cred-mech
use-auth-secret
static-auth-secret=<shared-secret-from-secrets-manager>
no-multicast-peers
no-cli
cert=/etc/letsencrypt/live/turn.<public_domain>/fullchain.pem
pkey=/etc/letsencrypt/live/turn.<public_domain>/privkey.pem
```

The `use-auth-secret` / `static-auth-secret` pair is the Synapse
integration point: Synapse uses the same shared secret to sign
short-lived (24h) credentials via HMAC, hands them to clients,
and coturn validates them without per-user state. No Synapse-to-
coturn API call at runtime.

Port range `49152-49251` (100 ports) is enough for a household;
each concurrent relay session consumes one. The full IANA-ephemeral
range (49152-65535) is the textbook setting, but each port has to
be open in the security group and that's a 16k-rule SG, which
exceeds AWS limits. 100-port relay range is the practical default.

### TURN credential plumbing into Synapse

`homeserver.yaml` additions (rendered from the existing init.sh
template):

```yaml
turn_uris:
  - "turn:turn.<public_domain>?transport=udp"
  - "turn:turn.<public_domain>?transport=tcp"
  - "turns:turn.<public_domain>?transport=tcp"
turn_shared_secret: "<same-secret-as-coturn>"
turn_user_lifetime: "1h"
turn_allow_guests: false
```

Shared secret lives in `matrix/turn-shared-secret` in Secrets
Manager. coturn reads it at boot (cloud-init pulls it via the
instance role); Synapse reads it at init time (the existing
ECS-secret pattern). Both consume the same field.

### TLS certificate

`turns:` requires a real cert (Element won't trust self-signed).
Options compared in **Open decisions** below; the leading
candidate is Lambda-rotated ACM/Let's Encrypt placed on EFS the
coturn host mounts, but ACM-on-NLB is also workable.

### CDK shape (TURN)

- `infra/stacks/turn_stack.py` — modeled on OpenClawStack:
  - `ec2.Instance` (Ubuntu, `t3.micro` or `t3.small`) in the
    PUBLIC subnet with an Elastic IP.
  - SecurityGroup: 3478/udp, 3478/tcp, 5349/tcp from 0.0.0.0/0,
    49152-49251/udp from 0.0.0.0/0.
  - cloud-init pulls the shared secret from Secrets Manager via
    instance role, writes `turnserver.conf`, starts coturn under
    systemd, hooks up certbot for cert renewal.
  - StandardBackupPlan on the EBS root (keeps the cert material).
- `infra/models/turn_config.py` — subdomain (`turn`), instance
  type, optional `relay_port_range` (default `49152-49251`).
- `config.toml` — new `[turn]` block.
- Synapse change: extend `MatrixImports` with `turn_uris`,
  `turn_shared_secret`, `turn_user_lifetime` and render them in
  the existing init.sh template alongside the other knobs.

## Building blocks — Phase 2 (Element-Call)

### LiveKit SFU

LiveKit is the SFU Element-Call uses. Self-host it as a Fargate
service with public-facing UDP. Key challenges:

- **UDP exposure.** LiveKit needs an externally-reachable UDP
  port range for media. NLB supports UDP listeners (one per port
  or port-range, since 2023) — `7881/tcp` for signaling,
  `7882/udp` for ICE/STUN, optional `50000-50100/udp` for TURN
  fallback. Alternative: host LiveKit on EC2 alongside coturn
  (same instance, different ports) — simpler networking, no NLB.
- **Auth.** LiveKit issues short-lived JWTs to clients. The JWTs
  are minted by `lk-jwt-service`, which checks the requester's
  Matrix OpenID token against Synapse before signing.

### lk-jwt-service

Small Go service (element-hq/lk-jwt-service). Stateless. Reads:

```
LIVEKIT_KEY=<api-key>            # shared with livekit-server
LIVEKIT_SECRET=<api-secret>      # shared with livekit-server
LIVEKIT_URL=wss://livekit.<public_domain>
LIVEKIT_JWT_PORT=8080
```

Two secrets, both in Secrets Manager. Fronted by `PublicHttpAlb`
at `lk-jwt.<public_domain>`. No EFS, no state.

### Element-Call frontend

Same shape as `ElementWebStack`: pull the `element-call` GitHub
release tarball, drop in `config.json`, deploy to S3 + CloudFront
at `call.<public_domain>`. config.json fields:

```json
{
  "default_server_config": {
    "m.homeserver": { "base_url": "https://matrix.<public_domain>" }
  },
  "livekit": {
    "livekit_service_url": "https://lk-jwt.<public_domain>"
  }
}
```

### Synapse experimental features

`homeserver.yaml`:

```yaml
experimental_features:
  msc3266_enabled: true   # room summary API, used by call UI
  msc3401_enabled: true   # group call signaling via room state
  msc4140_enabled: true   # delayed events (call invites)
serve_server_wellknown: true
```

Plus a `.well-known/matrix/client` entry served by ApexEdgeStack:

```json
{
  "io.element.call": {
    "preferred_domain": "https://call.<public_domain>"
  }
}
```

Element-Web reads this and uses our Element-Call instance instead
of `call.element.io`.

### CDK shape (Element-Call)

- `infra/stacks/element_call_stack.py` — Element-Call frontend,
  modeled on `ElementWebStack`. Pinned to `us-east-1`.
- `infra/stacks/livekit_stack.py` — LiveKit SFU + lk-jwt-service.
  Hosting choice is one of the open decisions below.
- `infra/models/element_call_config.py`, `livekit_config.py`.
- `config.toml` — `[element_call]`, `[livekit]` blocks.
- Synapse changes piped through `MatrixImports` and rendered
  into the homeserver.yaml template.
- ApexEdgeStack — extra `.well-known/matrix/client` content
  deployment with the `io.element.call.preferred_domain` field.

## Open decisions

These are the calls worth making together before execution. Each
has a leading candidate but is genuinely a trade-off.

### D1. TURN host: EC2 vs Fargate behind NLB

| Option | Pros | Cons |
|---|---|---|
| **EC2 + EIP** (like OpenClaw) | Trivial networking — UDP port range is just an SG rule, EIP gives a stable public IP, no LB billing. cloud-init is well-trodden in this repo. | EC2 ops surface (patching, instance health, recovery on AZ failure). |
| **Fargate + NLB** | Matches the rest of the stack. Auto-recovery on task failure. | NLB charges per LCU including idle (~$16/mo baseline). UDP port-range listeners are still single-port-per-listener in some regions; need to verify. EFS write-locking around cert renewal becomes a problem with >1 task. |
| **Managed (Cloudflare Calls / Xirsys)** | Zero ops. | Recurring cost ($5-15/mo), another vendor in the trust chain. |

**Leading candidate: EC2.** Personal-scale, no HA requirement,
matches the OpenClaw pattern. Cheaper than NLB. The "patching"
con is real but cloud-init + unattended-upgrades handles it.

**Operator decision: Agreed**

### D2. TURN cert provisioning

`turns:` (TLS-TURN on 5349) needs a real cert. coturn reads cert
files from disk and re-reads on SIGHUP.

| Option | Pros | Cons |
|---|---|---|
| **certbot on the EC2 host** | Standard. Renewal cron just works. Uses HTTP-01 or DNS-01. | Cert lives on the EC2 root volume; if the instance is replaced, certbot re-issues from scratch. |
| **ACM cert exported via Lambda** to S3, instance pulls at boot + on schedule | Cert centrally managed. | ACM can't export RSA private keys for public certs — only ACM Private CA does that. Dead end. |
| **Let's Encrypt via Lambda + S3** (manual ACME client) | Cert reusable across replacements. | Significantly more code than certbot-on-host. |

**Leading candidate: certbot on the host with DNS-01 against
Route53** (works without exposing port 80, and Route53 access is
just an instance role). Renewal cron does the SIGHUP into coturn.

**Operator decision: Agreed**

### D3. LiveKit host: EC2 (co-located with coturn) vs Fargate

| Option | Pros | Cons |
|---|---|---|
| **Co-locate on the TURN EC2 instance** | Single instance, single public IP, no NLB. Easier UDP. | Instance becomes a SPOF for *all* media. coturn + livekit-server contend for CPU/network during a busy call. |
| **Separate Fargate service + NLB** | Independent scaling, independent failure domain. | NLB cost. UDP port-range support varies; need to verify in `us-west-2`. |
| **Separate EC2 instance** | Same UDP simplicity. | Two boxes to manage. |

**Leading candidate: separate EC2 instance.** TURN and LiveKit
have orthogonal failure modes (TURN for relay traversal vs SFU
for forwarding) and decoupling makes per-component upgrade
trivial. Both are small `t3.micro`/`t3.small` boxes.

If running both on one instance is the chosen path, decide before
Phase 1 so the SG and EIP can be sized for both.

**Operator decision: Co-locate on the TURN EC2 instance.** We'll start with less
infrastructure, and consider a separate EC2 instance if we ever want to
partition our resource utilization.

### D4. Phase 1 only, or wait for Phase 2?

| Option | Pros | Cons |
|---|---|---|
| **Phase 1 immediately, Phase 2 later** | Unblocks 1:1 calls now for ~30 min of work. Phase 2 is much larger and can wait for an actual group-call need. | Two deploy cycles. |
| **Phase 1 + 2 bundled** | One end-to-end voice/video story. | Long-running branch; Element-Call has rough edges (MSC3401 is still experimental). |
| **Skip Phase 1, do Element-Call (which subsumes 1:1)** | Single deploy. | Element-Call's 1:1 UX is worse than the built-in Element-Web caller, and still needs a TURN server underneath, so Phase 1 doesn't actually go away. |

**Leading candidate: Phase 1 now, Phase 2 deferred** until a
real "I need to do a group call" moment. Bookmark Phase 2 as a
follow-up in this doc rather than a separate plan file.

**Operator decision: Phase 1 + 2 bundled**

### D5. Synapse `turn_user_lifetime`

Synapse docs default `1h`. Element-Web doesn't re-fetch
credentials mid-call, so a long call could outlive its
credentials. `24h` is the practical max coturn supports without
issues.

**Leading candidate: 24h.** Personal scale, the per-user
ephemerality is a defense against a credential leak we don't
realistically face.

**Operator decision: Agreed**

### D6. Restrict TURN to authenticated Matrix users only?

coturn supports allowlisting peer IPs (`allowed-peer-ip`) but
not "must be a Matrix-issued credential." The credentials *are*
HMAC-of-shared-secret + timestamp + userid, so a leaked credential
can be replayed for `turn_user_lifetime`. The shared secret
itself rotating would invalidate all live creds.

**Leading candidate: no extra restriction.** The HMAC scheme is
the standard. Address abuse if it ever materializes.

**Operator decision: Agreed**

## Sequencing

Phase 1 only, for now:

1. `infra/models/turn_config.py`, `infra/stacks/turn_stack.py`,
   `assets/turn/turnserver.conf.tmpl`, `assets/turn/user-data.sh.tmpl`.
2. `config.toml` `[turn]` block. `app_builder.py` instantiates
   `TurnStack`, passes `turn_shared_secret_name` +
   `turn_uri` into `MatrixImports`.
3. `MatrixImports` accepts the new fields; `MatrixStack` adds the
   secret to `init_secrets` and renders `turn_uris` /
   `turn_shared_secret` / `turn_user_lifetime` into the
   homeserver.yaml in `init.sh`.
4. Bootstrap: `bin/aws-write-secret matrix/turn-shared-secret -`
   with `{"secret":"<openssl rand -hex 32>"}`.
5. Deploy `TurnStack` first, then `MatrixStack` (so Synapse picks
   up the secret + URIs).

## Verification

- `dig +short turn.<public_domain>` resolves to the EIP.
- `nc -uvz turn.<public_domain> 3478` returns "succeeded"
  (UDP can be tricky; an alternative is the coturn-bundled
  `turnutils_uclient` against the public IP).
- From a Matrix client (Element-Web), pull /voip/turnServer:
  ```
  curl -H "Authorization: Bearer <access-token>" \
    https://matrix.<public_domain>/_matrix/client/v3/voip/turnServer
  ```
  Returns `{ "uris": [...], "username": "<ts>:@user", "password": "...", "ttl": 86400 }`.
- Place a 1:1 Element-Web call between two people, one of whom is
  on a mobile hotspot (forces TURN relay). Both sides see media.
- coturn logs (`/var/log/turnserver.log`) show the relay
  allocation; should be ~1 allocation per call leg.

## Out of scope / follow-ups

- **Phase 2 (Element-Call).** Documented above; defer until the
  household actually needs >2-party calls. When picked up, the
  `livekit_stack.py` + `element_call_stack.py` shells are the
  natural starting points and most of the open decisions
  (D3, the well-known wiring) are settled here.
- **TURN rate-limiting / quota.** coturn supports per-user
  bandwidth caps. Not relevant at household scale.
- **TURN federation with other Matrix servers.** Synapse's TURN
  is local to its own users — federated peers use *their*
  homeserver's TURN. No work needed on our side.
- **WebRTC stats / monitoring.** coturn exposes Prometheus
  metrics; out of scope until there's an actual monitoring stack.
- **IPv6.** coturn supports it; AWS public-subnet IPv6 needs the
  VPC to be dual-stack. Not currently set up.
