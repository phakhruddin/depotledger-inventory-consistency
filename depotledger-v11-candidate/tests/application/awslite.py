"""Minimal AWS client for DynamoDB and S3, standard library only.

The supplied images deliberately avoid third-party packages so an image build
can never fail on a package index. Requests are SigV4 signed so the images
behave the same against any AWS-compatible endpoint.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


class AwsError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


class _Signer:
    def __init__(self, region: str) -> None:
        self.region = region
        self.access_key = os.environ.get("AWS_ACCESS_KEY_ID", "test")
        self.secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "test")

    def headers(self, method: str, url: str, service: str, body: bytes,
                extra: dict[str, str] | None = None) -> dict[str, str]:
        parsed = urllib.parse.urlsplit(url)
        now = _dt.datetime.now(_dt.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest()
        headers = {
            "host": parsed.netloc,
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_hash,
        }
        for key, value in (extra or {}).items():
            headers[key.lower()] = value
        signed = sorted(headers)
        canonical_headers = "".join(f"{k}:{headers[k].strip()}\n" for k in signed)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        canonical_query = "&".join(
            f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
            for k, v in sorted(query)
        )
        canonical = "\n".join([
            method, parsed.path or "/", canonical_query, canonical_headers,
            ";".join(signed), payload_hash,
        ])
        scope = f"{date_stamp}/{self.region}/{service}/aws4_request"
        to_sign = "\n".join([
            "AWS4-HMAC-SHA256", amz_date, scope,
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        ])
        key = _sign(_sign(_sign(_sign(("AWS4" + self.secret_key).encode(), date_stamp),
                                self.region), service), "aws4_request")
        signature = hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        headers["authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={';'.join(signed)}, Signature={signature}"
        )
        headers.pop("host")
        return headers


def _send(method: str, url: str, headers: dict[str, str], body: bytes,
          timeout: float) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body if method in ("POST", "PUT") else None,
                                     method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


class DynamoDB:
    def __init__(self, endpoint: str, region: str, timeout: float = 10.0) -> None:
        self.endpoint = endpoint.rstrip("/") + "/"
        self.signer = _Signer(region)
        self.timeout = timeout

    def call(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        headers = self.signer.headers("POST", self.endpoint, "dynamodb", body, {
            "content-type": "application/x-amz-json-1.0",
            "x-amz-target": f"DynamoDB_20120810.{operation}",
        })
        status, _, raw = _send("POST", self.endpoint, headers, body, self.timeout)
        data: dict[str, Any] = {}
        if raw:
            try:
                data = json.loads(raw)
            except ValueError:
                data = {"message": raw[:300].decode("utf-8", "replace")}
        if status >= 300:
            code = str(data.get("__type", "UnknownError")).split("#")[-1]
            message = data.get("message") or data.get("Message") or ""
            raise AwsError(status, code, str(message))
        return data


class S3:
    def __init__(self, endpoint: str, region: str, timeout: float = 15.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.signer = _Signer(region)
        self.timeout = timeout

    def _url(self, bucket: str, key: str = "", query: dict[str, str] | None = None) -> str:
        path = f"/{bucket}"
        if key:
            path += "/" + urllib.parse.quote(key, safe="/-_.~")
        url = self.endpoint + path
        if query:
            url += "?" + urllib.parse.urlencode(sorted(query.items()))
        return url

    def _request(self, method: str, url: str, body: bytes = b"",
                 extra: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        headers = self.signer.headers(method, url, "s3", body, extra)
        status, response_headers, raw = _send(method, url, headers, body, self.timeout)
        if status >= 300:
            code, message = "UnknownError", raw[:300].decode("utf-8", "replace")
            try:
                root = ET.fromstring(raw)
                code = root.findtext("Code") or code
                message = root.findtext("Message") or message
            except ET.ParseError:
                pass
            raise AwsError(status, code, message)
        return status, response_headers, raw

    def put_object(self, bucket: str, key: str, body: bytes,
                   metadata: dict[str, str] | None = None,
                   content_type: str = "application/octet-stream") -> dict[str, str]:
        extra = {"content-type": content_type}
        for name, value in (metadata or {}).items():
            extra[f"x-amz-meta-{name}"] = value
        _, headers, _ = self._request("PUT", self._url(bucket, key), body, extra)
        return headers

    def get_object(self, bucket: str, key: str,
                   version_id: str | None = None) -> tuple[bytes, dict[str, str]]:
        query = {"versionId": version_id} if version_id else None
        _, headers, raw = self._request("GET", self._url(bucket, key, query))
        return raw, headers

    def head_object(self, bucket: str, key: str) -> dict[str, str]:
        _, headers, _ = self._request("HEAD", self._url(bucket, key))
        return headers

    def list_keys(self, bucket: str, prefix: str) -> list[str]:
        keys: list[str] = []
        token = None
        while True:
            query = {"list-type": "2", "prefix": prefix}
            if token:
                query["continuation-token"] = token
            _, _, raw = self._request("GET", self._url(bucket, query=query))
            root = ET.fromstring(raw)
            for node in root.iter():
                if node.tag in (f"{S3_NS}Contents", "Contents"):
                    key = node.findtext(f"{S3_NS}Key") or node.findtext("Key")
                    if key:
                        keys.append(key)
            truncated = (root.findtext(f"{S3_NS}IsTruncated") or root.findtext("IsTruncated") or "")
            token = root.findtext(f"{S3_NS}NextContinuationToken") or root.findtext("NextContinuationToken")
            if truncated.lower() != "true" or not token:
                return keys


# -- DynamoDB attribute value helpers --------------------------------------

def to_attr(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float)):
        return {"N": str(value)}
    if value is None:
        return {"NULL": True}
    return {"S": str(value)}


def from_attr(value: dict[str, Any]) -> Any:
    if "S" in value:
        return value["S"]
    if "N" in value:
        number = value["N"]
        return int(number) if number.lstrip("-").isdigit() else float(number)
    if "BOOL" in value:
        return bool(value["BOOL"])
    if "NULL" in value:
        return None
    if "M" in value:
        return {k: from_attr(v) for k, v in value["M"].items()}
    if "L" in value:
        return [from_attr(v) for v in value["L"]]
    return value


def to_item(record: dict[str, Any]) -> dict[str, Any]:
    return {k: to_attr(v) for k, v in record.items() if v is not None}


def from_item(item: dict[str, Any]) -> dict[str, Any]:
    return {k: from_attr(v) for k, v in item.items()}
