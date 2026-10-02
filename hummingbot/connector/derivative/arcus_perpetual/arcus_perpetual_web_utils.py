from typing import Optional

import hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_constants as CONSTANTS
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.web_assistant.auth import AuthBase
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest
from hummingbot.core.web_assistant.rest_pre_processors import RESTPreProcessorBase
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory


def public_rest_url(path_url: str, domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    return f"{CONSTANTS.REST_URLS[domain]}{path_url}"


def private_rest_url(path_url: str, domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    return public_rest_url(path_url, domain)


def public_ws_url(domain: str = CONSTANTS.DEFAULT_DOMAIN) -> str:
    return CONSTANTS.WS_URLS[domain]


class ArcusRESTPreProcessor(RESTPreProcessorBase):
    async def pre_process(self, request: RESTRequest) -> RESTRequest:
        headers = dict(request.headers or {})
        headers["User-Agent"] = CONSTANTS.USER_AGENT
        request.headers = headers
        return request


def build_api_factory(
    throttler: Optional[AsyncThrottler] = None,
    auth: Optional[AuthBase] = None,
) -> WebAssistantsFactory:
    return WebAssistantsFactory(
        throttler=throttler or create_throttler(),
        auth=auth,
        rest_pre_processors=[ArcusRESTPreProcessor()],
    )


def create_throttler() -> AsyncThrottler:
    return AsyncThrottler(CONSTANTS.RATE_LIMITS)


async def get_current_server_time(
    throttler: Optional[AsyncThrottler] = None,
    domain: str = CONSTANTS.DEFAULT_DOMAIN,
) -> float:
    api_factory = build_api_factory(throttler=throttler)
    rest_assistant = await api_factory.get_rest_assistant()
    response = await rest_assistant.execute_request(
        url=public_rest_url(CONSTANTS.TIME_PATH, domain=domain),
        method=RESTMethod.GET,
        throttler_limit_id=CONSTANTS.TIME_LIMIT_ID,
    )
    if not isinstance(response, dict) or "timeNs" not in response:
        raise IOError(f"Unexpected Arcus time response: {response}")
    return int(response["timeNs"]) * 1e-6
