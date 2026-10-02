import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from hummingbot.connector.derivative.arcus_perpetual import arcus_perpetual_web_utils as web_utils
from hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_auth import ArcusPerpetualAuth
from hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_constants import DEFAULT_DOMAIN
from hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_user_stream_data_source import (
    ArcusPerpetualUserStreamDataSource,
)
from hummingbot.connector.test_support.network_mocking_assistant import NetworkMockingAssistant


class ArcusPerpetualUserStreamDataSourceTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def create_data_source(api_factory: MagicMock | None = None) -> ArcusPerpetualUserStreamDataSource:
        return ArcusPerpetualUserStreamDataSource(
            auth=ArcusPerpetualAuth(None),
            account_address="0x" + "ab" * 20,
            account_index=3,
            api_factory=api_factory or MagicMock(),
            domain=DEFAULT_DOMAIN,
        )

    async def test_subscribes_all_account_channels_with_subaccount_identity(self):
        source = self.create_data_source()
        websocket = MagicMock()
        websocket.send = AsyncMock()

        await source._subscribe_channels(websocket)

        messages = [
            call.args[0].payload
            for call in websocket.send.await_args_list
        ]
        self.assertEqual(
            ["account", "positions", "userFills", "orders", "funding", "accountAttributeUpdates"],
            [message["channel"] for message in messages],
        )
        self.assertTrue(
            all(
                message["type"] == "subscribe"
                and message["id"] == "0x" + "ab" * 20
                and message["accountIndex"] == 3
                for message in messages
            )
        )

    async def test_user_stream_connection_uses_the_multiplexed_arcus_socket(self):
        websocket = MagicMock()
        websocket.connect = AsyncMock()
        api_factory = MagicMock()
        api_factory.get_ws_assistant = AsyncMock(return_value=websocket)
        source = self.create_data_source(api_factory)

        connected = await source._connected_websocket_assistant()

        self.assertIs(websocket, connected)
        websocket.connect.assert_awaited_once()
        self.assertEqual("wss://api.arcus.xyz/v1/ws", websocket.connect.await_args.kwargs["ws_url"])
        self.assertEqual(
            "OpenAI File Downloader, XaiImageApiFetch/1.0",
            websocket.connect.await_args.kwargs["ws_headers"]["User-Agent"],
        )


class ArcusPerpetualUserStreamNetworkTests(unittest.IsolatedAsyncioTestCase):
    mocking_assistant: NetworkMockingAssistant | None = None

    async def asyncSetUp(self) -> None:
        self.mocking_assistant = NetworkMockingAssistant()

    async def test_subscriptions_and_account_message_use_mocked_socket(self):
        assert self.mocking_assistant is not None
        mocking_assistant = self.mocking_assistant
        auth = ArcusPerpetualAuth(None)
        api_factory = web_utils.build_api_factory(auth=auth)
        websocket_mock = mocking_assistant.configure_web_assistants_factory(api_factory)
        address = "0x" + "ab" * 20
        source = ArcusPerpetualUserStreamDataSource(
            auth=auth,
            account_address=address,
            account_index=3,
            api_factory=api_factory,
            domain=DEFAULT_DOMAIN,
        )

        websocket = await source._connected_websocket_assistant()
        try:
            await source._subscribe_channels(websocket)
            sent_messages = mocking_assistant.json_messages_sent_through_websocket(websocket_mock)
            self.assertEqual(
                ["account", "positions", "userFills", "orders", "funding", "accountAttributeUpdates"],
                [message["channel"] for message in sent_messages],
            )
            self.assertTrue(
                all(
                    message["type"] == "subscribe"
                    and message["id"] == address
                    and message["accountIndex"] == 3
                    for message in sent_messages
                )
            )

            account_snapshot = {
                "type": "subscribed",
                "channel": "account",
                "id": address,
                "accountIndex": 3,
                "contents": {"equity": "120", "freeCollateral": "80"},
            }
            mocking_assistant.add_websocket_aiohttp_message(
                websocket_mock,
                json.dumps(account_snapshot),
            )

            response = await websocket.receive()
            if response is None:
                self.fail("Expected account snapshot over mocked Arcus websocket.")

            self.assertEqual(account_snapshot, response.data)
        finally:
            await websocket.disconnect()
