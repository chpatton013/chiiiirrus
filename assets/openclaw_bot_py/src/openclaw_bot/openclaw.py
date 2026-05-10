from __future__ import annotations

import aiohttp

# HTTP client for the loopback OpenClaw gateway.
#
# Mirrors the TypeScript bot's `forwardToGateway`. The exact
# request/response shape is tentative (POST /v1/chat with
# {"prompt": ...}); adjust here once the live daemon's API is
# confirmed.


async def forward_to_gateway(
    gateway_url: str,
    gateway_token: str,
    prompt: str,
) -> str:
    url = f"{gateway_url.rstrip('/')}/v1/chat"
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {gateway_token}",
            },
            json={"prompt": prompt},
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                raise RuntimeError(f"gateway HTTP {resp.status}: {body[:500]}")
            data = await resp.json()
    if err := data.get("error"):
        raise RuntimeError(err)
    for key in ("response", "reply", "output"):
        if (out := data.get(key)) is not None:
            return out
    raise RuntimeError(f"gateway returned no message field: {data!r}")
