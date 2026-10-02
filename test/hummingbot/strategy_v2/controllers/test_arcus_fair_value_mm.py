import asyncio
from decimal import Decimal

import yaml

from controllers.market_making.arcus_fair_value_mm import ArcusFairValueMMConfig, ArcusFairValueMMController
from hummingbot.client import settings
from hummingbot.core.data_type.common import PositionMode
from hummingbot.strategy.strategy_v2_base import StrategyV2ConfigBase
from hummingbot.strategy_v2.controllers.market_making_controller_base import (
    MarketMakingControllerBase,
    MarketMakingControllerConfigBase,
)


class FakeMarketDataProvider:
    ready = True

    def __init__(self):
        self.rate_sources = []

    def initialize_rate_sources(self, sources):
        self.rate_sources.extend(sources)

    def initialize_candles_feed(self, _):
        pass


def test_arcus_native_controller_shell_uses_standard_market_making_bases_and_emits_no_actions():
    config = ArcusFairValueMMConfig(
        id="arcus-shell",
        trading_pair="SPY-USD",
        total_amount_quote=Decimal("50"),
    )

    assert isinstance(config, MarketMakingControllerConfigBase)
    assert config.connector_name == "arcus_perpetual"
    assert config.leverage == 1
    assert config.position_mode is PositionMode.ONEWAY
    assert config.quote_notional == Decimal("50")
    assert config.observation_only is True
    assert config.get_controller_class() is ArcusFairValueMMController

    provider = FakeMarketDataProvider()
    controller = ArcusFairValueMMController(
        config=config,
        market_data_provider=provider,
        actions_queue=asyncio.Queue(),
    )

    assert isinstance(controller, MarketMakingControllerBase)
    assert controller.determine_executor_actions() == []
    assert [(p.connector_name, p.trading_pair) for p in provider.rate_sources] == [
        ("arcus_perpetual", "SPY-USD")
    ]


def test_arcus_controller_loads_through_standard_v2_controller_loader(tmp_path, monkeypatch):
    config_path = tmp_path / "conf_arcus.yml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "id": "arcus-loader",
                "controller_type": "market_making",
                "controller_name": "arcus_fair_value_mm",
                "connector_name": "arcus_perpetual",
                "trading_pair": "SPY-USD",
                "total_amount_quote": "50",
                "quote_notional": "50",
                "observation_only": True,
            }
        )
    )
    monkeypatch.setattr(settings, "CONTROLLERS_CONF_DIR_PATH", tmp_path)

    loader_config = StrategyV2ConfigBase(controllers_config=[config_path.name])
    [loaded] = loader_config.load_controller_configs()

    assert isinstance(loaded, ArcusFairValueMMConfig)
    assert loaded.get_controller_class() is ArcusFairValueMMController
