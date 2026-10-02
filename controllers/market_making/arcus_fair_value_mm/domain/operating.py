from enum import Enum


class OperatingState(str, Enum):
    GOOD = "GOOD"
    RISK_PAUSED = "RISK_PAUSED"
    FAULT = "FAULT"
    VOLATILE = "VOLATILE"
    TOXIC = "TOXIC"
