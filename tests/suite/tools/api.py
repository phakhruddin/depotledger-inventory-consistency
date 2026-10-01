"""HTTP client for the inventory API, through the public load balancer.

Every request carries the manifest's host header. Responses are returned raw
so a check can assert on status, headers and body.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import requests


@dataclass
class ApiResponse:
    status: int
    headers: dict[str, str]
    text: str

    @property
    def replica(self) -> str:
        return self.headers.get("x-depotledger-replica", "")

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except ValueError:
            return None

    def code(self) -> str:
        body = self.json()
        return body.get("code", "") if isinstance(body, dict) else ""


class Api:
    def __init__(self, base_url: str, host_header: str, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.host_header = host_header
        self.timeout = timeout

    def request(self, method: str, path: str, body: Any = None) -> ApiResponse:
        headers = {"Host": self.host_header}
        # A fresh connection per call, so concurrent callers are spread across
        # replicas instead of pinned to one keep-alive socket.
        headers["Connection"] = "close"
        response = requests.request(
            method, f"{self.base_url}{path}", headers=headers,
            json=body, timeout=self.timeout, allow_redirects=False,
        )
        return ApiResponse(
            status=response.status_code,
            headers={k.lower(): v for k, v in response.headers.items()},
            text=response.text,
        )

    def get(self, path: str) -> ApiResponse:
        return self.request("GET", path)

    def put(self, path: str, body: Any) -> ApiResponse:
        return self.request("PUT", path, body)

    def post(self, path: str, body: Any) -> ApiResponse:
        return self.request("POST", path, body)

    def delete(self, path: str) -> ApiResponse:
        return self.request("DELETE", path)
