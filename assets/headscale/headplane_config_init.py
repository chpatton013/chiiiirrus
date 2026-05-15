"""Render /etc/headplane/config.yaml for the headplane main container.

Headplane v0.6+ refuses to start without this file. Headplane
accepts JSON wherever it accepts YAML, so we emit a JSON dump
instead of templating YAML by hand -- avoids string-quoting + key
ordering hazards.

Inputs (env): COOKIE, APIKEY, CLIENT_ID, CLIENT_SECRET,
HP_HEADSCALE_URL, HP_OIDC_ISSUER. The init shell extracts the
first four from Secrets Manager and exports them before invoking
this script.
"""

import json
import os

cfg = {
    "server": {
        "host": "0.0.0.0",
        "port": 3000,
        "cookie_secret": os.environ["COOKIE"],
        "cookie_secure": False,
        "data_path": "/tmp/headplane/",
    },
    "headscale": {
        "url": os.environ["HP_HEADSCALE_URL"],
        "config_strict": False,
    },
    "oidc": {
        "issuer": os.environ["HP_OIDC_ISSUER"],
        "client_id": os.environ["CLIENT_ID"],
        "client_secret": os.environ["CLIENT_SECRET"],
        "token_endpoint_auth_method": "client_secret_basic",
        "disable_api_key_login": False,
        "headscale_api_key": os.environ["APIKEY"],
    },
}
with open("/etc/headplane/config.yaml", "w") as f:
    f.write(json.dumps(cfg, indent=2))
print("headplane config.yaml written")
