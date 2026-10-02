from hummingbot.connector.derivative.arcus_perpetual import arcus_perpetual_constants as CONSTANTS
from hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_web_utils import public_ws_url
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import WSJSONRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant


class ArcusPerpetualUserStreamDataSource(UserStreamTrackerDataSource):
    def __init__(
        self,
        auth: AuthBase,
        account_address: str | None,
        account_index: int,
        api_factory: WebAssistantsFactory,
        domain: str = CONSTANTS.DEFAULT_DOMAIN,
    ) -> None:
        super().__init__()
        self._auth = auth
        self._account_address = account_address
        self._account_index = account_index
        self._api_factory = api_factory
        self._domain = domain

    async def _connected_websocket_assistant(self) -> WSAssistant:
        websocket_assistant = await self._api_factory.get_ws_assistant()
        await websocket_assistant.connect(
            ws_url=public_ws_url(self._domain),
            ws_headers={"User-Agent": CONSTANTS.USER_AGENT},
            ping_timeout=30,
        )
        return websocket_assistant

    async def _subscribe_channels(self, websocket_assistant: WSAssistant):
        if not self._account_address:
            raise ValueError("Arcus account address is required for user-stream subscriptions.")
        address = self._account_address.lower()
        for channel in ("account", "positions", "userFills", "orders", "funding", "accountAttributeUpdates"):
            await websocket_assistant.send(
                WSJSONRequest(
                    {
                        "type": "subscribe",
                        "channel": channel,
                        "id": address,
                        "accountIndex": self._account_index,
                    }
                )
            )
