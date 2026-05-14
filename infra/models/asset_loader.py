import functools
import pathlib


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

    @functools.cache
    def read_text(self, *parts: str) -> str:
        return (self._assets.joinpath(*parts)).read_text()
