import functools
import pathlib
import re

# Stack templates place each substitutable value between
# `@@KEY@@` markers. The keys are uppercase ASCII + underscores;
# `render_template` rejects any other shape so an embedded "@@" in
# a config (e.g. `auth@@example.com`) doesn't get treated as a
# half-finished placeholder.
_PLACEHOLDER_PATTERN = re.compile(r"@@([A-Z][A-Z0-9_]*)@@")


class AssetLoader:
    def __init__(self, repo_root: pathlib.Path) -> None:
        self._assets = repo_root / "assets"

    def lambda_path(self, name: str) -> pathlib.Path:
        path = self._assets / "lambdas" / name
        if not path.is_dir():
            raise FileNotFoundError(f"lambda asset not found: {path}")
        return path

    def docker_path(self, name: str) -> pathlib.Path:
        path = self._assets / name
        if not (path / "Dockerfile").is_file():
            raise FileNotFoundError(f"docker asset not found: {path}/Dockerfile")
        return path

    def blueprints_path(self, name: str) -> pathlib.Path:
        path = self._assets / name / "blueprints"
        if not path.is_dir():
            raise FileNotFoundError(f"blueprints asset not found: {path}")
        return path

    def site_path(self) -> pathlib.Path:
        path = self._assets / "site"
        if not path.is_dir():
            raise FileNotFoundError(f"site asset not found: {path}")
        return path

    def openclaw_bot_path(self) -> pathlib.Path:
        path = self._assets / "openclaw_bot"
        if not path.is_dir():
            raise FileNotFoundError(f"openclaw_bot asset not found: {path}")
        return path

    def element_web_cache_path(self) -> pathlib.Path:
        return self._assets / "element-web" / "cache"

    def element_call_cache_path(self) -> pathlib.Path:
        return self._assets / "element-call" / "cache"

    def turn_assets_path(self) -> pathlib.Path:
        path = self._assets / "turn"
        if not path.is_dir():
            raise FileNotFoundError(f"turn assets not found: {path}")
        return path

    @functools.cache
    def read_text(self, *parts: str) -> str:
        return (self._assets.joinpath(*parts)).read_text()

    def render_template(self, *parts: str, substitutions: dict[str, str]) -> str:
        """Read a template asset and substitute its `@@KEY@@` markers.

        Strict on both sides: raises if `substitutions` contains keys
        the template doesn't reference, or if the template references
        keys `substitutions` doesn't provide. Either is a sign of
        drift between the template and its caller.
        """
        text = self.read_text(*parts)
        path = self._assets.joinpath(*parts)
        present = set(_PLACEHOLDER_PATTERN.findall(text))
        provided = set(substitutions)
        missing = present - provided
        unused = provided - present
        problems = []
        if missing:
            problems.append(
                f"template references {sorted(missing)} but no value was provided"
            )
        if unused:
            problems.append(
                f"substitutions {sorted(unused)} do not appear in the template"
            )
        if problems:
            raise ValueError(f"render_template({path}): " + "; ".join(problems))
        for key, value in substitutions.items():
            text = text.replace(f"@@{key}@@", value)
        return text
