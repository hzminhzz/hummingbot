from unittest import IsolatedAsyncioTestCase, TestCase

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_auth import ArcusPerpetualAuth
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest, WSJSONRequest


class ArcusPerpetualAuthTests(TestCase):
    SIGNING_KEY = bytes(range(32)).hex()
    AUTH = ArcusPerpetualAuth(SIGNING_KEY)

    def test_typed_order_payload_signs_compact_sorted_engine_payload(self):
        payload = {
            "op": 1,
            "ad": "0xabc",
            "q": 250,
            "ct": 1712345678000000000,
            "ai": 0,
        }
        signature = self.AUTH.sign_typed_payload(payload)
        public_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32))).public_key()

        self.assertEqual(128, len(signature))
        public_key.verify(
            bytes.fromhex(signature),
            b'{"ad":"0xabc","ai":0,"ct":1712345678000000000,"op":1,"q":250}',
        )

    def test_api_key_is_raw_hex_ed25519_public_key(self):
        expected = Ed25519PrivateKey.from_private_bytes(bytes(range(32))).public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ).hex()

        self.assertEqual(expected, self.AUTH.api_key)

    def test_authentication_headers_keep_nanosecond_timestamp(self):
        headers = self.AUTH.rest_headers(1712345678000000000, "a" * 128)

        self.assertEqual(self.AUTH.api_key, headers["X-API-Key"])
        self.assertEqual("1712345678000000000", headers["X-Timestamp"])
        self.assertEqual("a" * 128, headers["X-Signature"])

    def test_rejects_private_key_with_wrong_size(self):
        with self.assertRaisesRegex(ValueError, "32-byte hex"):
            ArcusPerpetualAuth("01")

    def test_rejects_non_hex_private_key(self):
        with self.assertRaisesRegex(ValueError, "32-byte hex"):
            ArcusPerpetualAuth("z" * 64)

    def test_legacy_leverage_signature_uses_timestamp_action_and_canonical_body(self):
        timestamp_ns = 1712345678000000000
        payload = {"marketId": 1, "leverage": 5, "address": "0xabc"}
        signature = self.AUTH.sign_legacy_payload(timestamp_ns, "setLeverage", payload)
        canonical_body = b'{"address":"0xabc","leverage":5,"marketId":1}'
        message = str(timestamp_ns).encode() + b"setLeverage" + canonical_body
        public_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32))).public_key()

        public_key.verify(bytes.fromhex(signature), message)

    def test_public_only_auth_cannot_sign_private_request(self):
        with self.assertRaisesRegex(ValueError, "required for authenticated trading"):
            ArcusPerpetualAuth(None).sign_message("payload")


class ArcusPerpetualAuthAsyncTests(IsolatedAsyncioTestCase):
    async def test_rest_auth_adds_public_key_and_preserves_signed_headers(self):
        auth = ArcusPerpetualAuth(bytes(range(32)).hex())
        request = RESTRequest(
            method=RESTMethod.POST,
            url="https://api.arcus.xyz/v1/placeOrder",
            headers={
                "X-Timestamp": "1712345678000000000",
                "X-Signature": "f" * 128,
            },
        )

        authenticated = await auth.rest_authenticate(request)

        headers = dict(authenticated.headers or {})
        self.assertEqual(auth.api_key, headers["X-API-Key"])
        self.assertEqual("1712345678000000000", headers["X-Timestamp"])
        self.assertEqual("f" * 128, headers["X-Signature"])

    async def test_ws_authentication_leaves_public_subscription_unchanged(self):
        auth = ArcusPerpetualAuth(None)
        request = WSJSONRequest({"type": "subscribe", "channel": "account", "id": "0xabc"})

        authenticated = await auth.ws_authenticate(request)

        self.assertIs(request, authenticated)
