"""SRP 移植正确性：客户端与服务端用同一组参数真实跑一遍握手。

这里不是"照抄常量"式的测试：服务端用 `compute_verifier` 建 verifier，独立算出 B、u、S、K，
再比对 M1/M2。端口写错（比如 M1 拼装顺序、u 的取值、N 的补零）都会让 M1 不一致。
"""

import os

import pytest

from icloud_api.srp.client import new_srp_client
from icloud_api.srp.params import (
    calculate_k,
    calculate_m1,
    calculate_m2,
    calculate_u,
    compute_verifier,
    get_multiplier,
    get_params,
)
from icloud_api.srp.utils import (
    bytes_from_hex_string,
    int_from_bytes,
    int_to_bytes,
    pad_to,
    pad_to_n,
)

IDENTITY = b"fengye@example.com"
PASSWORD = b"correct horse battery staple"


def _server_side(params, salt: bytes, b_value: int, verifier: int, client_a: int):
    """服务端：B = (k*v + g^b) mod N，S = (A * v^u)^b mod N。"""
    k = get_multiplier(params)
    b_bytes = pad_to_n((pow(params.g, b_value, params.n) + k * verifier) % params.n, params.n_length_bits)
    u = calculate_u(params, client_a, int_from_bytes(b_bytes))
    session = pad_to_n(
        pow(client_a * pow(verifier, u, params.n) % params.n, b_value, params.n), params.n_length_bits
    )
    return b_bytes, u, session, calculate_k(params, session)


@pytest.mark.parametrize("bits", [1024, 1536, 2048, 4096])
def test_m1_m2_agree_between_client_and_server(bits: int) -> None:
    params = get_params(bits)
    salt = os.urandom(16)
    verifier = int_from_bytes(compute_verifier(params, salt, IDENTITY, PASSWORD))
    b_value = int_from_bytes(os.urandom(32))

    client = new_srp_client(params, os.urandom(32))
    b_bytes, _, session, k_bytes = _server_side(params, salt, b_value, verifier, client.a)

    client.process_client_challenge(IDENTITY, PASSWORD, salt, b_bytes)

    assert client.get_session_key() == k_bytes
    assert client.s == int_from_bytes(session)
    expected_m1 = calculate_m1(params, IDENTITY, salt, client.get_a_bytes(), b_bytes, k_bytes)
    assert client.get_m1_bytes() == expected_m1
    client.check_m2(calculate_m2(params, client.get_a_bytes(), expected_m1, k_bytes))
    with pytest.raises(ValueError):
        client.check_m2(b"\x00" * len(expected_m1))


def test_wrong_password_does_not_match_server_verifier() -> None:
    params = get_params(2048)
    salt = os.urandom(16)
    verifier = int_from_bytes(compute_verifier(params, salt, IDENTITY, PASSWORD))
    b_value = int_from_bytes(os.urandom(32))

    client = new_srp_client(params, os.urandom(32))
    b_bytes, _, _, k_bytes = _server_side(params, salt, b_value, verifier, client.a)
    client.process_client_challenge(IDENTITY, b"wrong password", salt, b_bytes)

    assert client.get_session_key() != k_bytes


def test_known_group_shapes_match_apple_expectations() -> None:
    params_2048 = get_params(2048)
    assert params_2048.hash_name == "sha256"
    assert params_2048.g == 2
    assert int_to_bytes(params_2048.n).__len__() == 256
    assert int_to_bytes(get_params(4096).n).__len__() == 512
    with pytest.raises(ValueError):
        get_params(512)


def test_rejects_out_of_range_server_value() -> None:
    params = get_params(2048)
    client = new_srp_client(params, os.urandom(32))
    for bad_b in (b"\x00" * 256, pad_to_n(params.n, params.n_length_bits)):
        with pytest.raises(ValueError, match="invalid server-supplied 'B'"):
            client.process_client_challenge(IDENTITY, PASSWORD, os.urandom(16), bad_b)


def test_helpers_behave_like_the_go_originals() -> None:
    assert pad_to(b"\x01", 4) == b"\x00\x00\x00\x01"
    assert int_to_bytes(0) == b""
    assert int_from_bytes(b"") == 0
    assert bytes_from_hex_string("DE AD|be ef") == bytes.fromhex("deadbeef")
    with pytest.raises(ValueError):
        pad_to(b"\x01\x02", 1)
