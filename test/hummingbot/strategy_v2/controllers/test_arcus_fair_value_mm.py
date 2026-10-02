import asyncio
from decimal import Decimal
from types import SimpleNamespace

import yaml
from arcus_mm.external_reference import ExternalReferenceQuote
from arcus_mm.microstructure import FillSide, MicrostructureState
from arcus_mm.operating import OperatingState
from arcus_mm.quote_policy import FairValueState, MarketState, QuotePolicy, QuotePolicyConfig

from controllers.market_making.arcus_fair_value_mm import ArcusFairValueMMConfig, ArcusFairValueMMController
from controllers.market_making.arcus_fair_value_mm.domain.quote_policy import (
    FairValueState as NativeFairValueState,
    MarketState as NativeMarketState,
    MicrostructureState as NativeMicrostructureState,
    QuotePolicy as NativeQuotePolicy,
    QuotePolicyConfig as NativeQuotePolicyConfig,
    RestingQuote,
    RiskState as NativeRiskState,
)
from hummingbot.client import settings
from hummingbot.core.data_type.common import PositionMode, PositionSide, PriceType, TradeType
from hummingbot.core.event.events import OrderBookTradeEvent
from hummingbot.strategy.strategy_v2_base import StrategyV2ConfigBase
from hummingbot.strategy_v2.controllers.market_making_controller_base import (
    MarketMakingControllerBase,
    MarketMakingControllerConfigBase,
)
from hummingbot.strategy_v2.executors.order_executor.data_types import ExecutionStrategy, OrderExecutorConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, StopExecutorAction


def decision_signature(decision):
    return (
        decision.bid.action.value,
        decision.bid.price,
        decision.bid.size,
        decision.bid.reason,
        decision.ask.action.value,
        decision.ask.price,
        decision.ask.size,
        decision.ask.reason,
        decision.state.value,
        decision.state_reason,
    )


def make_inventory_policy():
    return NativeQuotePolicy(
        NativeQuotePolicyConfig(
            quote_notional=Decimal("100"),
            max_quote_deviation_bps=Decimal("100"),
            max_reference_disagreement_bps=Decimal("100"),
            max_abs_inventory=Decimal("0.15"),
        )
    )


def make_inventory_market(min_order_size=Decimal("0.001"), min_notional_size=Decimal("5")):
    return NativeMarketState(
        symbol="SPY-USD",
        bid=Decimal("750"),
        ask=Decimal("751"),
        oracle=Decimal("750.5"),
        mark=Decimal("750.5"),
        observed_at_ns=1_000_000_000,
        sequence_id=1,
        min_order_size=min_order_size,
        min_notional_size=min_notional_size,
    )


def make_inventory_fair_value():
    return NativeFairValueState(
        price=Decimal("750.5"),
        observed_at_ns=1_000_000_000,
        stale_after_ns=5_000_000_000,
        source="test",
        confidence=Decimal("1"),
    )


def decide_inventory(position):
    return make_inventory_policy().decide(
        market=make_inventory_market(),
        fair=make_inventory_fair_value(),
        now_ns=1_000_000_000,
        risk=NativeRiskState(
            position_base=Decimal(position),
            market_pnl=Decimal("0"),
            account_pnl=Decimal("0"),
        ),
    )


def test_projected_inventory_guard_allows_full_quote_at_flat_position():
    decision = decide_inventory("0")

    assert decision.bid.size is not None
    assert decision.ask.size is not None
    assert decision.bid.size == Decimal("100") / Decimal("750")
    assert decision.ask.size == Decimal("100") / Decimal("751")


def test_projected_inventory_guard_blocks_another_full_bid_after_long_fill():
    decision = decide_inventory("0.13")

    assert decision.bid.size is not None
    assert decision.bid.size == Decimal("0.02")
    assert decision.bid.size < Decimal("100") / Decimal("750")


def test_projected_inventory_guard_caps_worsening_quote_to_remaining_capacity():
    decision = decide_inventory("0.13")

    assert decision.bid.size is not None
    assert decision.bid.size == Decimal("0.15") - Decimal("0.13")
    assert Decimal("0.13") + decision.bid.size == Decimal("0.15")


def test_projected_inventory_guard_resizes_resting_worsening_quote():
    decision = make_inventory_policy().decide(
        market=make_inventory_market(),
        fair=make_inventory_fair_value(),
        now_ns=1_000_000_000,
        resting={"bid": RestingQuote(price=Decimal("750"), size=Decimal("100") / Decimal("750"))},
        risk=NativeRiskState(
            position_base=Decimal("0.13"),
            market_pnl=Decimal("0"),
            account_pnl=Decimal("0"),
        ),
    )

    assert decision.bid.action.value == "MOVE"
    assert decision.bid.size == Decimal("0.02")


def test_projected_inventory_guard_keeps_risk_reducing_ask_eligible_when_long():
    decision = decide_inventory("0.13")

    assert decision.ask.action.value == "PLACE"
    assert decision.ask.size is not None
    assert decision.ask.size == Decimal("100") / Decimal("751")


def test_projected_inventory_guard_mirrors_behavior_for_short_inventory():
    decision = decide_inventory("-0.13")

    assert decision.ask.size is not None
    assert decision.bid.size is not None
    assert decision.ask.size == Decimal("0.02")
    assert decision.bid.action.value == "PLACE"
    assert decision.bid.size == Decimal("100") / Decimal("750")


def test_projected_inventory_guard_never_exceeds_cap_and_suppresses_below_minimum():
    for position in ("-0.15", "-0.149", "-0.13", "0", "0.13", "0.149", "0.15"):
        decision = decide_inventory(position)
        current = Decimal(position)
        if decision.bid.size is not None:
            assert abs(current + decision.bid.size) <= Decimal("0.15")
        if decision.ask.size is not None:
            assert abs(current - decision.ask.size) <= Decimal("0.15")

    near_limit = make_inventory_policy().decide(
        market=make_inventory_market(min_notional_size=Decimal("5")),
        fair=make_inventory_fair_value(),
        now_ns=1_000_000_000,
        risk=NativeRiskState(
            position_base=Decimal("0.149"),
            market_pnl=Decimal("0"),
            account_pnl=Decimal("0"),
        ),
    )
    assert near_limit.bid.action.value == "HOLD"
    assert near_limit.bid.reason == "INVENTORY_LIMIT_BELOW_MINIMUM"

    below_minimum_size = make_inventory_policy().decide(
        market=make_inventory_market(
            min_order_size=Decimal("0.002"),
            min_notional_size=Decimal("0"),
        ),
        fair=make_inventory_fair_value(),
        now_ns=1_000_000_000,
        risk=NativeRiskState(
            position_base=Decimal("0.149"),
            market_pnl=Decimal("0"),
            account_pnl=Decimal("0"),
        ),
    )
    assert below_minimum_size.bid.action.value == "HOLD"
    assert below_minimum_size.bid.reason == "INVENTORY_LIMIT_BELOW_MINIMUM"


def test_projected_inventory_guard_preserves_toxicity_veto_priority():
    decision = make_inventory_policy().decide(
        market=make_inventory_market(),
        fair=make_inventory_fair_value(),
        now_ns=1_000_000_000,
        risk=NativeRiskState(
            position_base=Decimal("0.149"),
            market_pnl=Decimal("0"),
            account_pnl=Decimal("0"),
        ),
        microstructure=NativeMicrostructureState(
            volatility_bps=Decimal("0"),
            bid_toxic_override=True,
        ),
    )

    assert decision.bid.action.value == "HOLD"
    assert decision.bid.reason == "BID_TOXIC"


class FakeMarketDataProvider:
    ready = True

    def __init__(self):
        self.rate_sources = []
        self.bid = Decimal("100")
        self.ask = Decimal("101")
        self.oracle = Decimal("100.5")
        self.mark = Decimal("100.5")
        self.hl_bid = Decimal("1000")
        self.hl_ask = Decimal("1002")
        self.hl_update_id_ms = 1500
        self.order_book_initializations = []
        self.connector = None
        self.reference_last_recv_time = 2.0
        self.now = 2.0

    def initialize_rate_sources(self, sources):
        self.rate_sources.extend(sources)

    def initialize_candles_feed(self, _):
        pass

    def time(self):
        return self.now

    def get_price_by_type(self, connector_name, trading_pair, price_type):
        if connector_name == "arcus_perpetual":
            assert trading_pair == "SPY-USD"
            if price_type is PriceType.BestBid:
                return self.bid
            if price_type is PriceType.BestAsk:
                return self.ask
        elif connector_name == "hyperliquid_perpetual":
            assert trading_pair == "XYZ:SP500-USD"
            if price_type is PriceType.BestBid:
                return self.hl_bid
            if price_type is PriceType.BestAsk:
                return self.hl_ask
        raise AssertionError((connector_name, trading_pair, price_type))

    def get_funding_info(self, connector_name, trading_pair):
        return SimpleNamespace(index_price=self.oracle, mark_price=self.mark)

    def get_connector(self, connector_name):
        if self.connector is None:
            raise ValueError(f"Connector {connector_name} not found.")
        return self.connector

    def get_connector_with_fallback(self, connector_name):
        assert connector_name == "hyperliquid_perpetual"
        return SimpleNamespace(
            order_book_tracker=SimpleNamespace(
                data_source=SimpleNamespace(
                    _ws_assistant=SimpleNamespace(last_recv_time=self.reference_last_recv_time)
                )
            )
        )

    async def initialize_order_book(self, connector_name, trading_pair):
        self.order_book_initializations.append((connector_name, trading_pair))
        return True

    def get_order_book(self, connector_name, trading_pair):
        if connector_name == "arcus_perpetual":
            if not hasattr(self, "arcus_order_book"):
                raise AssertionError("Arcus order book not configured for this test")
            return self.arcus_order_book
        assert connector_name == "hyperliquid_perpetual"
        assert trading_pair == "XYZ:SP500-USD"
        return SimpleNamespace(
            snapshot_uid=self.hl_update_id_ms,
            last_diff_uid=self.hl_update_id_ms,
            get_price=lambda is_buy: self.hl_ask if is_buy else self.hl_bid,
        )


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


class FakeArcusOrderBook:
    def __init__(self, bid_amounts, ask_amounts):
        self.bid_amounts = [Decimal(str(x)) for x in bid_amounts]
        self.ask_amounts = [Decimal(str(x)) for x in ask_amounts]
        self.listeners = []

    def bid_entries(self):
        for i, amount in enumerate(self.bid_amounts):
            yield SimpleNamespace(price=100 - i, amount=amount)

    def ask_entries(self):
        for i, amount in enumerate(self.ask_amounts):
            yield SimpleNamespace(price=101 + i, amount=amount)

    def add_listener(self, event_tag, listener):
        self.listeners.append((event_tag, listener))

    def remove_listener(self, event_tag, listener):
        if (event_tag, listener) in self.listeners:
            self.listeners.remove((event_tag, listener))


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


def test_native_controller_builds_fresh_spy_fair_value_from_hyperliquid_anchor():
    provider = FakeMarketDataProvider()
    config = ArcusFairValueMMConfig(
        id="arcus-hl-reference",
        trading_pair="SPY-USD",
        quote_notional=Decimal("50"),
        max_quote_deviation_bps=Decimal("100"),
        max_reference_disagreement_bps=Decimal("100"),
        reference_stale_after_seconds=Decimal("5"),
        reference_anchor_spy_price=Decimal("100"),
        reference_anchor_hyperliquid_price=Decimal("1000"),
        reference_anchor_timestamp_ns=1_000_000_000,
        reference_anchor_max_age_seconds=Decimal("100"),
        reference_max_spread_bps=Decimal("30"),
    )
    controller = ArcusFairValueMMController(
        config=config,
        market_data_provider=provider,
        actions_queue=asyncio.Queue(),
    )

    asyncio.run(controller.update_processed_data())
    fair = controller.processed_data["fair_value_state"]
    decision = controller.processed_data["quote_decision"]

    assert fair.price == Decimal("100.1")
    assert fair.observed_at_ns == 2_000_000_000
    assert fair.source == "hyperliquid:XYZ:SP500-USD:anchored_ratio"
    assert decision.state.value == OperatingState.GOOD.value
    assert provider.order_book_initializations == [("hyperliquid_perpetual", "XYZ:SP500-USD")]

    asyncio.run(controller.update_processed_data())
    assert provider.order_book_initializations == [("hyperliquid_perpetual", "XYZ:SP500-USD")]


def test_hyperliquid_reference_uses_connector_health_not_price_change_frequency_and_fails_on_stale_anchor():
    provider = FakeMarketDataProvider()
    config = ArcusFairValueMMConfig(
        id="arcus-hl-stale",
        trading_pair="SPY-USD",
        quote_notional=Decimal("50"),
        max_quote_deviation_bps=Decimal("100"),
        max_reference_disagreement_bps=Decimal("100"),
        reference_stale_after_seconds=Decimal("1"),
        reference_anchor_spy_price=Decimal("100"),
        reference_anchor_hyperliquid_price=Decimal("1000"),
        reference_anchor_timestamp_ns=1_000_000_000,
        reference_anchor_max_age_seconds=Decimal("100"),
    )
    controller = ArcusFairValueMMController(
        config=config,
        market_data_provider=provider,
        actions_queue=asyncio.Queue(),
    )

    provider.now = 10.0
    provider.reference_last_recv_time = 10.0
    provider.hl_update_id_ms = 1_000
    asyncio.run(controller.update_processed_data())
    unchanged_book = controller.processed_data["quote_decision"]
    assert unchanged_book.state.value == OperatingState.GOOD.value
    assert controller.processed_data["fair_value_state"].observed_at_ns == 10_000_000_000

    provider.reference_last_recv_time = 8.0
    asyncio.run(controller.update_processed_data())
    disconnected = controller.processed_data["quote_decision"]
    assert disconnected.state.value == OperatingState.FAULT.value
    assert disconnected.state_reason == "INVALID_REFERENCE"
    assert controller.processed_data["fair_value_state"].source == "hyperliquid_sp500:STALE_PUBLIC_FEED"

    provider.reference_last_recv_time = 200.0
    provider.now = 200.0
    provider.hl_update_id_ms = 200_000
    asyncio.run(controller.update_processed_data())
    stale_anchor = controller.processed_data["quote_decision"]
    assert stale_anchor.state.value == OperatingState.FAULT.value
    assert stale_anchor.state_reason == "INVALID_REFERENCE"
    assert controller.processed_data["fair_value_state"].source == "hyperliquid_sp500:STALE_ANCHOR"


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
        risk=NativeRiskState(
            position_base=Decimal("0.5"),
            market_pnl=Decimal("-5"),
            account_pnl=Decimal("-7"),
        ),
    )

    assert decision_signature(decision) == decision_signature(expected)
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
    assert stale.state.value == OperatingState.FAULT.value
    assert stale.state_reason == "STALE_REFERENCE"

    controller._external_reference_client.observed_at_ns = 1_500_000_000
    asyncio.run(controller.update_processed_data())
    toxic = controller.processed_data["quote_decision"]
    assert toxic.state.value == OperatingState.TOXIC.value
    assert toxic.bid.action.value == "HOLD"
    assert toxic.bid.reason == "BID_TOXIC"
    assert toxic.ask.action.value == "PLACE"


def test_native_controller_uses_exchange_position_as_authoritative_restart_inventory():
    provider = FakeMarketDataProvider()
    provider.connector = SimpleNamespace(
        account_positions={
            "spy-short": SimpleNamespace(
                trading_pair="SPY-USD",
                position_side=PositionSide.SHORT,
                amount=Decimal("0.06"),
                unrealized_pnl=Decimal("-1.25"),
            )
        }
    )
    config = ArcusFairValueMMConfig(
        id="arcus-restart-risk",
        trading_pair="SPY-USD",
        quote_notional=Decimal("10"),
        max_quote_deviation_bps=Decimal("100"),
        max_reference_disagreement_bps=Decimal("100"),
        max_abs_inventory=Decimal("0.02"),
        inventory_skew_at_limit=Decimal("1"),
    )
    controller = ArcusFairValueMMController(
        config=config,
        market_data_provider=provider,
        actions_queue=asyncio.Queue(),
        external_reference_client=FakeExternalReference(),
    )

    asyncio.run(controller.update_processed_data())
    risk = controller.processed_data["risk_state"]
    decision = controller.processed_data["quote_decision"]

    assert risk.position_base == Decimal("-0.06")
    assert risk.market_pnl == Decimal("-1.25")
    assert decision.state_reason == "NORMAL"
    assert decision.ask.action.value == "HOLD"
    assert decision.ask.reason == "INVENTORY_LIMIT_BELOW_MINIMUM"
    assert decision.bid.action.value == "PLACE"


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
    assert volatile.state.value == OperatingState.VOLATILE.value
    assert volatile.bid.size == Decimal("25") / Decimal("100")

    controller.performance_report = SimpleNamespace(global_pnl_quote=Decimal("-50"))
    asyncio.run(controller.update_processed_data())
    paused = controller.processed_data["quote_decision"]
    assert paused.state.value == OperatingState.RISK_PAUSED.value
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
    assert controller.processed_data["quote_decision"].state.value == OperatingState.RISK_PAUSED.value
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


def test_mvp_reprice_hysteresis_preserves_queue_until_drift_exceeds_threshold():
    provider = FakeMarketDataProvider()
    external = FakeExternalReference(observed_at_ns=1_500_000_000)
    config = ArcusFairValueMMConfig(
        id="arcus-hysteresis",
        trading_pair="SPY-USD",
        quote_notional=Decimal("50"),
        max_quote_deviation_bps=Decimal("100"),
        max_reference_disagreement_bps=Decimal("100"),
        reprice_hysteresis_bps=Decimal("0.3"),
        observation_only=False,
    )
    controller = ArcusFairValueMMController(
        config=config,
        market_data_provider=provider,
        actions_queue=asyncio.Queue(),
        external_reference_client=external,
    )
    controller.executors_info = [
        make_active_quote_executor("bid-h", TradeType.BUY, Decimal("100"), Decimal("0.5")),
        make_active_quote_executor("ask-h", TradeType.SELL, Decimal("101"), Decimal("0.5")),
    ]

    provider.bid = Decimal("100.002")
    provider.ask = Decimal("101.002")
    asyncio.run(controller.update_processed_data())
    within = controller.processed_data["quote_decision"]
    assert within.bid.action.value == "KEEP"
    assert within.bid.reason == "BID_QUEUE_HYSTERESIS"
    assert within.ask.action.value == "KEEP"
    assert within.ask.reason == "ASK_QUEUE_HYSTERESIS"
    assert controller.determine_executor_actions() == []

    provider.bid = Decimal("100.01")
    provider.ask = Decimal("101.01")
    asyncio.run(controller.update_processed_data())
    moved = controller.determine_executor_actions()
    assert {type(action) for action in moved} == {StopExecutorAction}
    assert {action.executor_id for action in moved} == {"bid-h", "ask-h"}


def test_mvp_live_concordance_veto_suppresses_only_adverse_side():
    provider = FakeMarketDataProvider()
    provider.arcus_order_book = FakeArcusOrderBook(
        bid_amounts=[Decimal("10"), Decimal("8"), Decimal("6")],
        ask_amounts=[Decimal("1"), Decimal("1"), Decimal("1")],
    )
    config = ArcusFairValueMMConfig(
        id="arcus-concordance",
        trading_pair="SPY-USD",
        quote_notional=Decimal("50"),
        max_quote_deviation_bps=Decimal("100"),
        max_reference_disagreement_bps=Decimal("100"),
        enable_concordance_veto=True,
        concordance_window_seconds=Decimal("5"),
        concordance_book_levels=5,
        observation_only=False,
    )
    controller = ArcusFairValueMMController(
        config=config,
        market_data_provider=provider,
        actions_queue=asyncio.Queue(),
        external_reference_client=FakeExternalReference(observed_at_ns=1_500_000_000),
    )
    controller._on_public_trade(
        OrderBookTradeEvent(
            trading_pair="SPY-USD",
            timestamp=1.8,
            type=TradeType.BUY,
            price=Decimal("101"),
            amount=Decimal("2"),
        )
    )

    asyncio.run(controller.update_processed_data())
    micro = controller.processed_data["microstructure_state"]
    decision = controller.processed_data["quote_decision"]
    actions = controller.determine_executor_actions()

    assert micro.book_imbalance > 0
    assert micro.trade_imbalance > 0
    assert micro.ask_toxic_override is True
    assert micro.bid_toxic_override is False
    assert decision.bid.action.value == "PLACE"
    assert decision.ask.action.value == "HOLD"
    assert decision.ask.reason == "ASK_TOXIC"
    assert len(actions) == 1
    assert actions[0].executor_config.side is TradeType.BUY
    assert len(provider.arcus_order_book.listeners) == 1

    info = controller.get_custom_info()
    assert info["pair"] == "SPY-USD"
    assert info["book_imbalance"] == str(micro.book_imbalance)
    assert info["trade_imbalance"] == str(micro.trade_imbalance)
    assert info["ask_action"] == "HOLD"
    assert info["ask_reason"] == "ASK_TOXIC"
    assert info["ask_toxic_ratio"] == 1.0
    assert info["bid_eligible_ratio"] == 1.0


def test_mvp_market_data_fault_cancels_existing_quotes_fail_closed():
    provider = FakeMarketDataProvider()
    config = ArcusFairValueMMConfig(
        id="arcus-fault",
        trading_pair="SPY-USD",
        quote_notional=Decimal("50"),
        max_quote_deviation_bps=Decimal("100"),
        max_reference_disagreement_bps=Decimal("100"),
        observation_only=False,
    )
    controller = ArcusFairValueMMController(
        config=config,
        market_data_provider=provider,
        actions_queue=asyncio.Queue(),
        external_reference_client=FakeExternalReference(observed_at_ns=1_500_000_000),
    )
    controller.executors_info = [
        make_active_quote_executor("bid-f", TradeType.BUY, Decimal("100"), Decimal("0.5")),
        make_active_quote_executor("ask-f", TradeType.SELL, Decimal("101"), Decimal("0.5")),
    ]

    provider.ready = False
    asyncio.run(controller.update_processed_data())
    decision = controller.processed_data["quote_decision"]
    actions = controller.determine_executor_actions()

    assert decision.state.value == OperatingState.FAULT.value
    assert decision.state_reason == "MARKET_DATA_FAULT"
    assert {type(action) for action in actions} == {StopExecutorAction}
    assert {action.executor_id for action in actions} == {"bid-f", "ask-f"}
