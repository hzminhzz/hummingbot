import unittest

from pydantic import SecretStr, ValidationError

from hummingbot.client.settings import AllConnectorSettings, ConnectorType
from hummingbot.connector.derivative.arcus_perpetual.arcus_perpetual_utils import ArcusPerpetualConfigMap


class ArcusPerpetualUtilsTests(unittest.TestCase):
    def test_connector_is_discovered_as_perpetual_with_secrets_configured(self):
        settings = AllConnectorSettings.create_connector_settings()
        connector = settings["arcus_perpetual"]

        self.assertEqual(ConnectorType.Derivative, connector.type)
        self.assertEqual("BTC-USD", connector.example_pair)
        self.assertIn("api_signing_key", ArcusPerpetualConfigMap.model_fields)
        self.assertIn("account_address", ArcusPerpetualConfigMap.model_fields)
        self.assertIn("account_index", ArcusPerpetualConfigMap.model_fields)

    def test_account_index_is_limited_to_arcus_subaccounts(self):
        config = ArcusPerpetualConfigMap(
            api_signing_key=SecretStr("00" * 32),
            account_address="0x0000000000000000000000000000000000000001",
            account_index=9,
        )

        self.assertEqual(9, config.account_index)
        with self.assertRaises(ValidationError):
            ArcusPerpetualConfigMap(
                api_signing_key=SecretStr("00" * 32),
                account_address="0x0000000000000000000000000000000000000001",
                account_index=10,
            )
