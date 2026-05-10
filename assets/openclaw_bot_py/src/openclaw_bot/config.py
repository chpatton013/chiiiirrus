from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    homeserver_url: str
    access_token: str
    user_id: str
    device_id: str
    control_room_id: str | None
    control_room_param: str
    allowed_sender: str
    data_dir: Path
    gateway_url: str
    gateway_token: str
    rate_limit_max_per_window: int = 6
    rate_limit_window_seconds: float = 60.0


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required env: {name}")
    return value


def _read_file_env(name: str) -> str:
    path = _require_env(name)
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"{name} points at missing file: {path}")
    return p.read_text().strip()


def load_config() -> Config:
    data_dir = Path(_require_env("MATRIX_BOT_DATA_DIR"))
    data_dir.mkdir(parents=True, exist_ok=True)
    control_room_id = os.environ.get("CONTROL_ROOM_ID", "").strip() or None
    return Config(
        homeserver_url=_require_env("HOMESERVER_URL"),
        access_token=_read_file_env("BOT_ACCESS_TOKEN_FILE"),
        user_id=_require_env("BOT_USER_ID"),
        device_id=_require_env("BOT_DEVICE_ID"),
        control_room_id=control_room_id,
        control_room_param=_require_env("CONTROL_ROOM_PARAM"),
        allowed_sender=_require_env("ALLOWED_SENDER"),
        data_dir=data_dir,
        gateway_url=_require_env("OPENCLAW_GATEWAY_URL"),
        gateway_token=_read_file_env("OPENCLAW_GATEWAY_TOKEN_FILE"),
    )
