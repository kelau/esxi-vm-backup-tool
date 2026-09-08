from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from urllib.parse import quote

import httpx

from .models import PortainerConfig


class ResponseStream:
    def __init__(self, response: httpx.Response):
        self.response = response
        self.iterator = response.iter_raw()
        self.buffer = bytearray()

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            return bytes(self.buffer) + b"".join(self.iterator)
        while len(self.buffer) < size:
            try:
                self.buffer.extend(next(self.iterator))
            except StopIteration:
                break
        output = bytes(self.buffer[:size])
        del self.buffer[:size]
        return output


class PortainerClient:
    _usage_cache = {}
    _usage_lock = threading.Lock()

    def volume_usage(self, endpoint_id: int) -> dict:
        key = (self.config.url, endpoint_id)
        with self._usage_lock:
            cached_at, cached = self._usage_cache.get(key, (0, {}))
            if time.monotonic() - cached_at < 300:
                return cached
            try:
                result = self._request(
                    "GET", f"/api/endpoints/{endpoint_id}/docker/system/df"
                ).json()
                cached = {
                    volume["Name"]: (volume.get("UsageData") or {}).get("Size")
                    for volume in result.get("Volumes") or []
                }
            except httpx.HTTPError:
                cached = {}
            self._usage_cache[key] = (time.monotonic(), cached)
            return cached

    @staticmethod
    def persistent_size(mounts, usage):
        sizes = {}
        for mount in mounts:
            if mount.get("Type") == "bind":
                return None
            if mount.get("Type") != "volume":
                continue
            name = mount.get("Name")
            size = usage.get(name)
            if not isinstance(size, int) or size < 0:
                return None
            sizes[name] = size
        return sum(sizes.values())

    def __init__(self, config: PortainerConfig):
        self.config = config
        self.client: httpx.Client | None = None

    def __enter__(self):
        self.client = httpx.Client(
            base_url=self.config.url.rstrip("/"),
            headers={"X-API-Key": self.config.api_key.get_secret_value()},
            verify=self.config.verify_ssl,
            timeout=30,
        )
        return self

    def __exit__(self, *_):
        if self.client:
            self.client.close()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        response = self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response

    def endpoints(self) -> list[dict]:
        return [
            {"id": int(item["Id"]), "name": item["Name"], "status": item.get("Status")}
            for item in self._request("GET", "/api/endpoints").json()
            if item.get("Type") in {1, 2, 3, 4, 7}
        ]

    def containers(self, endpoint_id: int) -> list[dict]:
        items = self._request(
            "GET", f"/api/endpoints/{endpoint_id}/docker/containers/json",
            params={"all": 1, "size": 1},
        ).json()
        usage = self.volume_usage(endpoint_id)
        return [{
            "id": item["Id"], "name": (item.get("Names") or [item["Id"][:12]])[0].lstrip("/"),
            "image": item.get("Image", ""), "image_id": item.get("ImageID", ""),
            "state": item.get("State", "unknown"), "status": item.get("Status", ""),
            "container_bytes": item.get("SizeRw"),
            "persistent_bytes": self.persistent_size(item.get("Mounts", []), usage),
        } for item in items]

    def inspect_container(self, endpoint_id: int, container_id: str) -> dict:
        return self._request(
            "GET", f"/api/endpoints/{endpoint_id}/docker/containers/{container_id}/json"
        ).json()

    def inspect_volume(self, endpoint_id: int, name: str) -> dict:
        return self._request(
            "GET", f"/api/endpoints/{endpoint_id}/docker/volumes/{quote(name, safe='')}"
        ).json()

    @staticmethod
    def is_network_volume(volume: dict) -> bool:
        options = volume.get("Options") or {}
        filesystem = str(options.get("type", "")).lower()
        return filesystem in {"nfs", "nfs4", "cifs", "smb", "smb3"}

    def pause(self, endpoint_id: int, container_id: str) -> None:
        path = f"/api/endpoints/{endpoint_id}/docker/containers/{container_id}/pause"
        self._request("POST", path)

    def unpause(self, endpoint_id: int, container_id: str) -> None:
        path = f"/api/endpoints/{endpoint_id}/docker/containers/{container_id}/unpause"
        self._request("POST", path)

    @contextmanager
    def archive(self, endpoint_id: int, container_id: str, path: str):
        url = (
            f"/api/endpoints/{endpoint_id}/docker/containers/{container_id}/archive"
            f"?path={quote(path, safe='')}"
        )
        with self.client.stream("GET", url, timeout=None) as response:
            response.raise_for_status()
            yield ResponseStream(response)
