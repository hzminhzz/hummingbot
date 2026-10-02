import json
import unittest

from aioresponses import aioresponses

import hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_constants as CONSTANTS
from hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_web_utils import (
    build_api_factory,
    create_throttler,
    get_current_server_time,
    public_rest_url,
)
from hummingbot.core.web_assistant.connections.data_types import RESTMethod, RESTRequest


class ArcusPerpetualWebUtilsTests(unittest.IsolatedAsyncioTestCase):
    async def test_rest_factory_sets_required_user_agent_and_preserves_other_headers(self):
        request = RESTRequest(
            method=RESTMethod.GET,
            url="https://api.arcus.xyz/v1/markets",
            headers={"X-API-Key": "public-key"},
        )
        factory = build_api_factory()
        assistant = await factory.get_rest_assistant()

        processed = await assistant._pre_process_request(request)
        headers = dict(processed.headers or {})

        self.assertEqual("OpenAI File Downloader, XaiImageApiFetch/1.0", headers["User-Agent"])
        self.assertEqual("public-key", headers["X-API-Key"])

    async def test_hummingbot_default_request_paths_have_rate_limit_rules(self):
        throttler = create_throttler()

        async with throttler.execute_task(CONSTANTS.HEALTH_PATH):
            pass
        async with throttler.execute_task(CONSTANTS.MARKETS_PATH):
            pass

    @aioresponses()
    async def test_current_server_time_converts_nanoseconds_from_mocked_rest(self, mock_api):
        url = public_rest_url(CONSTANTS.TIME_PATH)
        mock_api.get(url, body=json.dumps({"timeNs": 1_700_000_000_123_456_000}))

        current_time = await get_current_server_time()

        self.assertAlmostEqual(1_700_000_000_123.456, current_time, places=3)
        mock_api.assert_called_once()
