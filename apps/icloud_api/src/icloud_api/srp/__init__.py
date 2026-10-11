"""SRP-6a 实现（对应 CYMail `internal/srp`）。"""

from icloud_api.srp.client import SRPClient, new_srp_client
from icloud_api.srp.params import (
    SRPParams,
    calculate_a,
    calculate_k,
    calculate_m1,
    calculate_m2,
    calculate_s,
    calculate_u,
    calculate_x,
    compute_verifier,
    get_multiplier,
    get_params,
)

__all__ = [
    "SRPClient",
    "SRPParams",
    "calculate_a",
    "calculate_k",
    "calculate_m1",
    "calculate_m2",
    "calculate_s",
    "calculate_u",
    "calculate_x",
    "compute_verifier",
    "get_multiplier",
    "get_params",
    "new_srp_client",
]
