import json
from typing import Any, Dict

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTRequest, WSRequest


class ArcusPerpetualAuth(AuthBase):
    def __init__(self, api_signing_key: str | None) -> None:
        if not api_signing_key:
            self._private_key = None
            self._api_key = ""
            return
        normalized_key = api_signing_key.removeprefix("0x").removeprefix("0X")
        try:
            private_key_bytes = bytes.fromhex(normalized_key)
        except ValueError as error:
            raise ValueError("Arcus API signing key must be 32-byte hex") from error
        if len(private_key_bytes) != 32:
            raise ValueError("Arcus API signing key must be 32-byte hex")
        self._private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
        self._api_key = self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ).hex()

    @property
    def api_key(self) -> str:
        return self._api_key

    def sign_message(self, message: str) -> str:
        if self._private_key is None:
            raise ValueError("Arcus API signing key is required for authenticated trading")
        return self._private_key.sign(message.encode("utf-8")).hex()

    def sign_typed_payload(self, payload: Dict[str, Any]) -> str:
        canonical_payload = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        return self.sign_message(canonical_payload)

    def sign_legacy_payload(self, timestamp_ns: int, action: str, payload: Dict[str, Any]) -> str:
        canonical_payload = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        return self.sign_message(f"{timestamp_ns}{action}{canonical_payload}")

    def rest_headers(self, timestamp_ns: int, signature: str) -> Dict[str, str]:
        return {
            "X-API-Key": self._api_key,
            "X-Timestamp": str(timestamp_ns),
            "X-Signature": signature,
        }

    async def rest_authenticate(self, request: RESTRequest) -> RESTRequest:
        headers = dict(request.headers or {})
        headers["X-API-Key"] = self._api_key
        request.headers = headers
        return request

    async def ws_authenticate(self, request: WSRequest) -> WSRequest:
        return request
