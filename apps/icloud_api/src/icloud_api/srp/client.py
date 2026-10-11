"""SRP 客户端（对应 CYMail `internal/srp/client.go`）。"""

import os
from dataclasses import dataclass, field

from icloud_api.srp.params import (
    SRPParams,
    calculate_a,
    calculate_k,
    calculate_m1,
    calculate_m2,
    calculate_s,
    calculate_u,
    calculate_x,
    get_multiplier,
)
from icloud_api.srp.utils import int_from_bytes, pad_to_n


@dataclass
class SRPClient:
    """`secret1` 是私钥指数 a；`a` 是公开值 A（整数），与 Go 字段命名一致。"""

    params: SRPParams
    secret1: int
    multiplier: int
    a: int
    x: int | None = None
    m1: bytes = b""
    m2: bytes = b""
    k: bytes = b""
    u: int | None = None
    s: int | None = None
    _a_bytes_cache: bytes | None = field(default=None, repr=False)

    def process_client_challenge(
        self, username: bytes, password: bytes, salt: bytes, b_bytes: bytes
    ) -> None:
        self.x = calculate_x(self.params, salt, username, password)
        b_value = int_from_bytes(b_bytes)
        u = calculate_u(self.params, self.a, b_value)
        s_bytes = calculate_s(
            self.params, self.multiplier, self.x, self.secret1, b_value, u
        )
        self.k = calculate_k(self.params, s_bytes)
        self.u = u
        self.s = int_from_bytes(s_bytes)
        a_bytes = self.get_a_bytes()
        self.m1 = calculate_m1(self.params, username, salt, a_bytes, b_bytes, self.k)
        self.m2 = calculate_m2(self.params, a_bytes, self.m1, self.k)

    def get_a_bytes(self) -> bytes:
        if self._a_bytes_cache is None:
            self._a_bytes_cache = pad_to_n(self.a, self.params.n_length_bits)
        return self._a_bytes_cache

    def get_m1_bytes(self) -> bytes:
        return self.m1

    def get_session_key(self) -> bytes:
        return self.k

    def check_m2(self, m2: bytes) -> None:
        if self.m2 != m2:
            raise ValueError("M2 didn't check")


def new_srp_client(params: SRPParams, secret: bytes | None = None) -> SRPClient:
    if not secret:
        secret = os.urandom(32)
    secret1 = int_from_bytes(secret)
    multiplier = get_multiplier(params)
    a_bytes = calculate_a(params, secret1)
    return SRPClient(
        params=params,
        secret1=secret1,
        multiplier=multiplier,
        a=int_from_bytes(a_bytes),
        _a_bytes_cache=a_bytes,
    )
