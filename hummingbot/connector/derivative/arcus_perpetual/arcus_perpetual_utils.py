from decimal import Decimal

from pydantic import ConfigDict, Field, SecretStr

from hummingbot.client.config.config_data_types import BaseConnectorConfigMap
from hummingbot.connector.derivative.arcus_perpetual import arcus_perpetual_constants as CONSTANTS
from hummingbot.core.data_type.trade_fee import TradeFeeSchema

CENTRALIZED = False
EXAMPLE_PAIR = "BTC-USD"

# Conservative estimate until account-specific live fee tiers are fetched.
DEFAULT_FEES = TradeFeeSchema(
    maker_percent_fee_decimal=Decimal("0.0005"),
    taker_percent_fee_decimal=Decimal("0.0005"),
)


class ArcusPerpetualConfigMap(BaseConnectorConfigMap):
    connector: str = CONSTANTS.EXCHANGE_NAME
    api_signing_key: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your Arcus Ed25519 API signing key (hex)",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )
    account_address: str = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter the master wallet address registered to the Arcus API key",
            "is_secure": False,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )
    account_index: int = Field(default=0, ge=0, le=9)
    use_testnet: bool = False
    model_config = ConfigDict(title=CONSTANTS.EXCHANGE_NAME)


KEYS = ArcusPerpetualConfigMap.model_construct()


class ArcusPerpetualTestnetConfigMap(BaseConnectorConfigMap):
    connector: str = CONSTANTS.TESTNET_DOMAIN
    api_signing_key: SecretStr = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter your Arcus Testnet Ed25519 API signing key (hex)",
            "is_secure": True,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )
    account_address: str = Field(
        default=...,
        json_schema_extra={
            "prompt": "Enter the master wallet address registered to the Arcus Testnet API key",
            "is_secure": False,
            "is_connect_key": True,
            "prompt_on_new": True,
        },
    )
    account_index: int = Field(default=0, ge=0, le=9)
    use_testnet: bool = True
    model_config = ConfigDict(title=CONSTANTS.TESTNET_DOMAIN)


OTHER_DOMAINS = [CONSTANTS.TESTNET_DOMAIN]
OTHER_DOMAINS_PARAMETER = {CONSTANTS.TESTNET_DOMAIN: CONSTANTS.TESTNET_DOMAIN}
OTHER_DOMAINS_EXAMPLE_PAIR = {CONSTANTS.TESTNET_DOMAIN: EXAMPLE_PAIR}
OTHER_DOMAINS_DEFAULT_FEES = {CONSTANTS.TESTNET_DOMAIN: DEFAULT_FEES}
OTHER_DOMAINS_KEYS = {CONSTANTS.TESTNET_DOMAIN: ArcusPerpetualTestnetConfigMap.model_construct()}
