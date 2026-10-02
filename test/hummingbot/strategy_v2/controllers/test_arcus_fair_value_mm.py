import asyncio
from decimal import Decimal
from types import SimpleNamespace

import yaml
from arcus_mm.external_reference import ExternalReferenceQuote
from arcus_mm.microstructure import FillSide, MicrostructureState
from arcus_mm.operating import OperatingState
from arcus_mm.quote_policy import FairValueState, MarketState, QuotePolicy, QuotePolicyConfig
from arcus_mm.risk import RiskState

from controllers.market_making.arcus_fair_value_mm import ArcusFairValueMMConfig, ArcusFairValueMMController
from hummingbot.client import settings
from hummingbot.core.data_type.common import PositionMode, PriceType, TradeType
from hummingbot.strategy.strategy_v2_base import StrategyV2ConfigBase
from hummingbot.strategy_v2.controllers.market_making_controller_base import (
    MarketMakingControllerBase,
    MarketMakingControllerConfigBase,
)
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, StopExecutorAction


class FakeMarketDataProvider:
    ready = True

    def __init__(self):
        self.rate_sources = []
        self.bid = Decimal("100")
        self.ask = Decimal("101")
        self.oracle = Decimal("100.5")
        self.mark = Decimal("100.5")
        self.now = 2.0

    def initialize_rate_sources(self, sources):
        self.rate_sources.extend(sources)

    def initialize_candles_feed(self, _):
        pass

    def time(self):
        return self.now

    def get_price_by_type(self, connector_name, trading_pair, price_type):
        assert connector_name == "arcus_perpetual"
        assert trading_pair == "SPY-USD"
        if price_type is PriceType.BestBid:
            return self.bid
        if price_type is PriceType.BestAsk:
            return self.ask
        raise AssertionError(price_type)

    def get_funding_info(self, connector_name, trading_pair):
        return SimpleNamespace(index_price=self.oracle, mark_price=self.mark)


class FakeExternalReference:
    def __init__(self, price=Decimal("100.5"), observed_at_ns=1_000_000_000):
        self.price = price
        self.observed_at_ns = observed_at_ns

    def get(self, symbol):
        return ExternalReferenceQuote(
            symbol=symbol,
            price=self.price,
            observed_at_ns=self.observed_at_ns,
            source="fake:SPY",
        )


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


def test_native_controller_reuses_quote_policy_for_market_reference_and_risk_parity():
    provider = FakeMarketDataProvider()
    external = FakeExternalReference()
    config = ArcusFairValueMMConfig(
        id="arcus-policy",
        trading_pair="SPY-USD",
        quote_notional=Decimal("50"),
        max_quote_deviation_bps=Decimal("100"),
        max_reference_disagreement_bps=Decimal("100"),
        reference_stale_after_seconds=Decimal("5"),
        max_abs_inventory=Decimal("1"),
        inventory_skew_at_limit=Decimal("0.5"),
        market_loss_limit=Decimal("20"),
        account_loss_limit=Decimal("50"),
    )
    controller = ArcusFairValueMMController(
        config=config,
        market_data_provider=provider,
        actions_queue=asyncio.Queue(),
        external_reference_client=external,
    )
    controller.positions_held = [
        SimpleNamespace(
            connector_name="arcus_perpetual",
            trading_pair="SPY-USD",
            side=TradeType.BUY,
            amount=Decimal("0.5"),
            global_pnl_quote=Decimal("-5"),
        )
    ]
    controller.performance_report = SimpleNamespace(global_pnl_quote=Decimal("-7"))

    asyncio.run(controller.update_processed_data())
    decision = controller.processed_data["quote_decision"]

    expected = QuotePolicy(
        QuotePolicyConfig(
            quote_notional=Decimal("50"),
            max_quote_deviation_bps=Decimal("100"),
            max_reference_disagreement_bps=Decimal("100"),
            max_abs_inventory=Decimal("1"),
            inventory_skew_at_limit=Decimal("0.5"),
            market_loss_limit=Decimal("20"),
            account_loss_limit=Decimal("50"),
        )
    ).decide(
        market=MarketState(
            symbol="SPY-USD",
            bid=Decimal("100"),
            ask=Decimal("101"),
            oracle=Decimal("100.5"),
            mark=Decimal("100.5"),
            observed_at_ns=2_000_000_000,
            sequence_id=2_000_000_000,
        ),
        fair=FairValueState(
            price=Decimal("100.5"),
            observed_at_ns=1_000_000_000,
            stale_after_ns=5_000_000_000,
            source="fake:SPY",
            confidence=Decimal("1"),
        ),
        now_ns=2_000_000_000,
        risk=RiskState(
            position_base=Decimal("0.5"),
            market_pnl=Decimal("-5"),
            account_pnl=Decimal("-7"),
        ),
    )

    assert decision == expected
    assert decision.bid.size < decision.ask.size
    assert controller.determine_executor_actions() == []


def test_native_controller_preserves_fail_closed_and_side_specific_toxicity_semantics():
    provider = FakeMarketDataProvider()
    stale_external = FakeExternalReference(observed_at_ns=0)
    config = ArcusFairValueMMConfig(
        id="arcus-policy",
        trading_pair="SPY-USD",
        quote_notional=Decimal("50"),
        max_quote_deviation_bps=Decimal("100"),
        max_reference_disagreement_bps=Decimal("100"),
        reference_stale_after_seconds=Decimal("1"),
        toxic_markout_threshold_bps=Decimal("-1"),
    )
    controller = ArcusFairValueMMController(
        config=config,
        market_data_provider=provider,
        actions_queue=asyncio.Queue(),
        external_reference_client=stale_external,
        microstructure_state_provider=lambda: MicrostructureState(
            volatility_bps=Decimal("0"),
            bid_recent_markout_bps=Decimal("-2"),
            ask_recent_markout_bps=Decimal("1"),
            previous_state=OperatingState.GOOD,
            previous_toxic_side=FillSide.BID,
        ),
    )

    asyncio.run(controller.update_processed_data())
    stale = controller.processed_data["quote_decision"]
    assert stale.state is OperatingState.FAULT
    assert stale.state_reason == "STALE_REFERENCE"

    controller._external_reference_client.observed_at_ns = 1_500_000_000
    asyncio.run(controller.update_processed_data())
    toxic = controller.processed_data["quote_decision"]
    assert toxic.state is OperatingState.TOXIC
    assert toxic.bid.action.value == "HOLD"
    assert toxic.bid.reason == "BID_TOXIC"
    assert toxic.ask.action.value == "PLACE"


def test_native_controller_exposes_risk_pause_and_volatility_size_reduction():
    provider = FakeMarketDataProvider()
    external = FakeExternalReference(observed_at_ns=1_500_000_000)
    micro = MicrostructureState(volatility_bps=Decimal("6"))
    config = ArcusFairValueMMConfig(
        id="arcus-states",
        trading_pair="SPY-USD",
        quote_notional=Decimal("50"),
        max_quote_deviation_bps=Decimal("100"),
        max_reference_disagreement_bps=Decimal("100"),
        reference_stale_after_seconds=Decimal("5"),
        account_loss_limit=Decimal("50"),
        volatile_threshold_bps=Decimal("5"),
        volatile_size_multiplier=Decimal("0.5"),
    )
    controller = ArcusFairValueMMController(
        config=config,
        market_data_provider=provider,
        actions_queue=asyncio.Queue(),
        external_reference_client=external,
        microstructure_state_provider=lambda: micro,
    )

    controller.performance_report = SimpleNamespace(global_pnl_quote=Decimal("0"))
    asyncio.run(controller.update_processed_data())
    volatile = controller.processed_data["quote_decision"]
    assert volatile.state is OperatingState.VOLATILE
    assert volatile.bid.size == Decimal("25") / Decimal("100")

    controller.performance_report = SimpleNamespace(global_pnl_quote=Decimal("-50"))
    asyncio.run(controller.update_processed_data())
    paused = controller.processed_data["quote_decision"]
    assert paused.state is OperatingState.RISK_PAUSED
    assert paused.state_reason == "ACCOUNT_LOSS_LIMIT"
    assert paused.bid.action.value == "HOLD"
    assert paused.ask.action.value == "HOLD"


def make_active_quote_executor(executor_id, side, price, amount):
    level_id = "arcus_bid" if side is TradeType.BUY else "arcus_ask"
    config = OrderExecutorConfig(
        timestamp=1.0,
        connector_name="arcus_perpetual",
        trading_pair="SPY-USD",
        side=side,
        amount=amount,
        price=price,
        execution_strategy=ExecutionStrategy.LIMIT_MAKER,
        leverage=1,
        level_id=level_id,
    )
    return SimpleNamespace(
        id=executor_id,
        is_active=True,
        config=config,
        custom_info={"level_id": level_id, "side": side},
    )


def test_native_actions_place_keep_move_cancel_hold_with_limit_maker_only():
    provider = FakeMarketDataProvider()
    external = FakeExternalReference(observed_at_ns=1_500_000_000)
    config = ArcusFairValueMMConfig(
        id="arcus-execution",
        trading_pair="SPY-USD",
        quote_notional=Decimal("50"),
        max_quote_deviation_bps=Decimal("100"),
        max_reference_disagreement_bps=Decimal("100"),
        reference_stale_after_seconds=Decimal("5"),
        observation_only=False,
    )
    controller = ArcusFairValueMMController(
        config=config,
        market_data_provider=provider,
        actions_queue=asyncio.Queue(),
        external_reference_client=external,
    )

    asyncio.run(controller.update_processed_data())
    place_actions = controller.determine_executor_actions()
    assert len(place_actions) == 2
    assert all(isinstance(action, CreateExecutorAction) for action in place_actions)
    created = {action.executor_config.side: action.executor_config for action in place_actions}
    assert set(created) == {TradeType.BUY, TradeType.SELL}
    assert all(isinstance(c, OrderExecutorConfig) for c in created.values())
    assert all(c.execution_strategy is ExecutionStrategy.LIMIT_MAKER for c in created.values())
    assert created[TradeType.BUY].level_id == "arcus_bid"
    assert created[TradeType.SELL].level_id == "arcus_ask"
    assert created[TradeType.BUY].price == Decimal("100")
    assert created[TradeType.SELL].price == Decimal("101")

    controller.executors_info = [
        make_active_quote_executor("bid-1", TradeType.BUY, Decimal("100"), created[TradeType.BUY].amount),
        make_active_quote_executor("ask-1", TradeType.SELL, Decimal("101"), created[TradeType.SELL].amount),
    ]
    asyncio.run(controller.update_processed_data())
    assert controller.processed_data["quote_decision"].bid.action.value == "KEEP"
    assert controller.processed_data["quote_decision"].ask.action.value == "KEEP"
    assert controller.determine_executor_actions() == []

    provider.bid = Decimal("99.5")
    provider.ask = Decimal("101.5")
    asyncio.run(controller.update_processed_data())
    move_actions = controller.determine_executor_actions()
    assert {type(action) for action in move_actions} == {StopExecutorAction}
    assert {action.executor_id for action in move_actions} == {"bid-1", "ask-1"}

    controller.executors_info = []
    asyncio.run(controller.update_processed_data())
    replacement_actions = controller.determine_executor_actions()
    assert len(replacement_actions) == 2
    assert all(isinstance(action, CreateExecutorAction) for action in replacement_actions)

    controller.executors_info = [
        make_active_quote_executor("bid-2", TradeType.BUY, Decimal("99.5"), Decimal("0.5")),
        make_active_quote_executor("ask-2", TradeType.SELL, Decimal("101.5"), Decimal("0.5")),
    ]
    controller._external_reference_client.observed_at_ns = 0
    provider.now = 10.0
    asyncio.run(controller.update_processed_data())
    cancel_actions = controller.determine_executor_actions()
    assert {type(action) for action in cancel_actions} == {StopExecutorAction}
    assert {action.executor_id for action in cancel_actions} == {"bid-2", "ask-2"}

    controller.executors_info = []
    asyncio.run(controller.update_processed_data())
    assert controller.processed_data["quote_decision"].bid.action.value == "HOLD"
    assert controller.processed_data["quote_decision"].ask.action.value == "HOLD"
    assert controller.determine_executor_actions() == []


def test_native_actions_respect_toxicity_and_risk_suppression():
    provider = FakeMarketDataProvider()
    external = FakeExternalReference(observed_at_ns=1_500_000_000)
    micro = MicrostructureState(
        volatility_bps=Decimal("0"),
        bid_recent_markout_bps=Decimal("-2"),
        ask_recent_markout_bps=Decimal("1"),
    )
    config = ArcusFairValueMMConfig(
        id="arcus-suppression",
        trading_pair="SPY-USD",
        quote_notional=Decimal("50"),
        max_quote_deviation_bps=Decimal("100"),
        max_reference_disagreement_bps=Decimal("100"),
        toxic_markout_threshold_bps=Decimal("-1"),
        account_loss_limit=Decimal("50"),
        observation_only=False,
    )
    controller = ArcusFairValueMMController(
        config=config,
        market_data_provider=provider,
        actions_queue=asyncio.Queue(),
        external_reference_client=external,
        microstructure_state_provider=lambda: micro,
    )

    controller.performance_report = SimpleNamespace(global_pnl_quote=Decimal("0"))
    asyncio.run(controller.update_processed_data())
    toxic_actions = controller.determine_executor_actions()
    assert len(toxic_actions) == 1
    assert isinstance(toxic_actions[0], CreateExecutorAction)
    assert toxic_actions[0].executor_config.side is TradeType.SELL
    assert toxic_actions[0].executor_config.execution_strategy is ExecutionStrategy.LIMIT_MAKER

    controller.performance_report = SimpleNamespace(global_pnl_quote=Decimal("-50"))
    asyncio.run(controller.update_processed_data())
    assert controller.processed_data["quote_decision"].state is OperatingState.RISK_PAUSED
    assert controller.determine_executor_actions() == []


def test_observation_mode_never_emits_native_executor_actions():
    provider = FakeMarketDataProvider()
    controller = ArcusFairValueMMController(
        config=ArcusFairValueMMConfig(
            id="arcus-observe",
            trading_pair="SPY-USD",
            quote_notional=Decimal("50"),
            max_quote_deviation_bps=Decimal("100"),
            max_reference_disagreement_bps=Decimal("100"),
            observation_only=True,
        ),
        market_data_provider=provider,
        actions_queue=asyncio.Queue(),
        external_reference_client=FakeExternalReference(observed_at_ns=1_500_000_000),
    )

    asyncio.run(controller.update_processed_data())
    assert controller.processed_data["quote_decision"].bid.action.value == "PLACE"
    assert controller.determine_executor_actions() == []
