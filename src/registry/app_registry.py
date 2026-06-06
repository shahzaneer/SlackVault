import os
import time
import asyncio
import logging
from typing import Optional, TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from ..aws.secrets_manager import SecretsManagerClient

logger = logging.getLogger(__name__)

REFRESH_COOLDOWN_SECONDS = 30


class AppRegistry:
    def __init__(self, config_path: Optional[str] = None, sm_client: Optional["SecretsManagerClient"] = None):
        if config_path is None:
            config_path = os.path.join(
                os.path.dirname(__file__), "..", "..", "config", "app-registry.yaml"
            )
        self.config_path = config_path
        self.sm_client = sm_client
        self._apps: list[dict] = []
        self._discovered_apps: list[dict] = []
        self._hybrid: list[dict] = []
        self._loaded_aws = False
        self._yaml_mtime: float = 0
        self._last_refresh_ts: float = 0
        self._refresh_lock = asyncio.Lock()
        self._load_yaml()

    def _load_yaml(self):
        if os.path.exists(self.config_path):
            self._yaml_mtime = os.path.getmtime(self.config_path)
            with open(self.config_path) as f:
                data = yaml.safe_load(f)
            self._apps = data.get("apps", []) if data else []
            logger.info("Loaded %d app entries from YAML registry", len(self._apps))
        else:
            logger.warning("App registry YAML not found at %s", self.config_path)
            self._apps = []

    def _yaml_changed(self) -> bool:
        if not os.path.exists(self.config_path):
            return False
        return os.path.getmtime(self.config_path) != self._yaml_mtime

    def discover_from_aws(self) -> list[str]:
        client = self.sm_client
        if not client:
            logger.warning("No SM client provided — cannot discover secrets from AWS")
            return []
        discovered_names = []
        secrets = client.discover_secret_names()
        for secret in secrets:
            full_name = secret["name"]
            aliases = self._generate_aliases(full_name)
            self._discovered_apps.append({
                "aliases": aliases,
                "secret_path": full_name,
                "source": "aws",
            })
            discovered_names.append(full_name)
        self._loaded_aws = True
        self._rebuild_hybrid()
        return discovered_names

    def needs_refresh(self) -> bool:
        if not self.sm_client or not self._loaded_aws:
            return False
        return (time.time() - self._last_refresh_ts) > REFRESH_COOLDOWN_SECONDS

    async def refresh(self):
        client = self.sm_client
        if not client or not self._loaded_aws:
            return

        async with self._refresh_lock:
            if (time.time() - self._last_refresh_ts) <= REFRESH_COOLDOWN_SECONDS:
                return

            if self._yaml_changed():
                logger.info("YAML registry changed — reloading")
                self._load_yaml()

            def _do_aws_refresh():
                client.refresh_cache()
                return client.discover_secret_names()

            secrets = await asyncio.to_thread(_do_aws_refresh)

            self._discovered_apps = []
            for secret in secrets:
                full_name = secret["name"]
                aliases = self._generate_aliases(full_name)
                self._discovered_apps.append({
                    "aliases": aliases,
                    "secret_path": full_name,
                    "source": "aws",
                })

            self._rebuild_hybrid()
            self._last_refresh_ts = time.time()

            logger.info(
                "Lazy refresh complete: %d YAML + %d AWS = %d hybrid entries",
                len(self._apps), len(self._discovered_apps), len(self._hybrid),
            )

    def _generate_aliases(self, name: str) -> list[str]:
        aliases = [name]
        last_segment = name.rstrip("/").rsplit("/", 1)[-1] if "/" in name else name
        if last_segment != name:
            aliases.append(last_segment)
        if last_segment.endswith("-service"):
            short = last_segment[:-8]
            aliases.append(short)
        if "-" in last_segment:
            spaced = last_segment.replace("-", " ")
            if spaced != last_segment:
                aliases.append(spaced)
            parts = last_segment.split("-")
            if len(parts) == 2 and parts[0] != parts[1]:
                aliases.append(parts[0])
            if parts and parts[-1] == "svc":
                aliases.append("-".join(parts[:-1]))
        if name.endswith("/"):
            aliases.append(name.rstrip("/"))
        return aliases

    def _rebuild_hybrid(self):
        seen_paths = set()
        self._hybrid = []
        for app in self._apps:
            path = app["secret_path"]
            if path not in seen_paths:
                self._hybrid.append({**app, "source": "yaml"})
                seen_paths.add(path)

        for app in self._discovered_apps:
            path = app["secret_path"]
            if path not in seen_paths:
                self._hybrid.append(app)
                seen_paths.add(path)

        logger.info("Hybrid registry: %d apps (%d from YAML, %d from AWS)",
                     len(self._hybrid), len(self._apps), len(self._discovered_apps))

    def resolve(self, app_name: str) -> Optional[str]:
        if not app_name:
            return None
        normalized = app_name.strip().lower()

        sources = self._hybrid if self._hybrid else self._apps

        for entry in sources:
            for alias in entry.get("aliases", []):
                if alias.lower() == normalized:
                    return entry["secret_path"]

        for entry in sources:
            for alias in entry.get("aliases", []):
                if self._levenshtein_distance(normalized, alias.lower()) <= 2:
                    return entry["secret_path"]

        return None

    def resolve_with_candidates(self, app_name: str) -> Optional[tuple[str, list[str]]]:
        if not app_name:
            return None
        normalized = app_name.strip().lower()

        sources = self._hybrid if self._hybrid else self._apps

        for entry in sources:
            for alias in entry.get("aliases", []):
                if alias.lower() == normalized:
                    return (entry["secret_path"], entry.get("aliases", [app_name]))

        candidates = []
        for entry in sources:
            for alias in entry.get("aliases", []):
                if self._levenshtein_distance(normalized, alias.lower()) <= 2:
                    return (entry["secret_path"], entry.get("aliases", [app_name]))
                if normalized in alias.lower() or alias.lower() in normalized:
                    candidates.append(entry)

        if len(candidates) == 1:
            entry = candidates[0]
            return (entry["secret_path"], entry.get("aliases", [app_name]))

        return None

    def get_known_apps(self) -> list[str]:
        sources = self._hybrid if self._hybrid else self._apps
        seen = set()
        result = []
        for entry in sources:
            path = entry["secret_path"]
            if path not in seen:
                result.append(path)
                seen.add(path)
        return result

    def get_known_app_names(self) -> list[str]:
        sources = self._hybrid if self._hybrid else self._apps
        names = set()
        for entry in sources:
            path = entry["secret_path"]
            short = path.rsplit("/", 1)[-1] if "/" in path else path
            short = short.replace("{environment}", "").strip("/")
            if short:
                names.add(short)
        return sorted(names)

    @staticmethod
    def _levenshtein_distance(s1: str, s2: str) -> int:
        if len(s1) < len(s2):
            return AppRegistry._levenshtein_distance(s2, s1)
        if len(s2) == 0:
            return len(s1)
        prev = list(range(len(s2) + 1))
        for i, c1 in enumerate(s1):
            curr = [i + 1]
            for j, c2 in enumerate(s2):
                cost = 0 if c1 == c2 else 1
                curr.append(min(
                    curr[j] + 1,
                    prev[j + 1] + 1,
                    prev[j] + cost,
                ))
            prev = curr
        return prev[-1]