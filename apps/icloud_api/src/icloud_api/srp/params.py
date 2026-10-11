"""SRP 已知群组与运算（对应 CYMail `internal/srp/params.go`）。

N/G、哈希算法与 M1/M2 的拼装顺序按原实现逐行保留：Apple 服务端用同一套值校验，
任何"顺手优化"（例如改用 RFC 5054 标准的 M1 或给 N 补零）都会让登录直接失败。
"""

import hashlib
from dataclasses import dataclass

from icloud_api.srp.utils import (
    bytes_from_hex_string,
    int_from_bytes,
    int_to_bytes,
    pad_to_n,
)

_N_1024 = """
    EEAF0AB9 ADB38DD6 9C33F80A FA8FC5E8 60726187 75FF3C0B 9EA2314C
    9C256576 D674DF74 96EA81D3 383B4813 D692C6E0 E0D5D8E2 50B98BE4
    8E495C1D 6089DAD1 5DC7D7B4 6154D6B6 CE8EF4AD 69B15D49 82559B29
    7BCF1885 C529F566 660E57EC 68EDBC3C 05726CC0 2FD4CBF4 976EAA9A
    FD5138FE 8376435B 9FC61D2F C0EB06E3
"""

_N_1536 = """
    9DEF3CAF B939277A B1F12A86 17A47BBB DBA51DF4 99AC4C80 BEEEA961
    4B19CC4D 5F4F5F55 6E27CBDE 51C6A94B E4607A29 1558903B A0D0F843
    80B655BB 9A22E8DC DF028A7C EC67F0D0 8134B1C8 B9798914 9B609E0B
    E3BAB63D 47548381 DBC5B1FC 764E3F4B 53DD9DA1 158BFD3E 2B9C8CF5
    6EDF0195 39349627 DB2FD53D 24B7C486 65772E43 7D6C7F8C E442734A
    F7CCB7AE 837C264A E3A9BEB8 7F8A2FE9 B8B5292E 5A021FFF 5E91479E
    8CE7A28C 2442C6F3 15180F93 499A234D CF76E3FE D135F9BB
"""

_N_2048 = """
    AC6BDB41 324A9A9B F166DE5E 1389582F AF72B665 1987EE07 FC319294
    3DB56050 A37329CB B4A099ED 8193E075 7767A13D D52312AB 4B03310D
    CD7F48A9 DA04FD50 E8083969 EDB767B0 CF609517 9A163AB3 661A05FB
    D5FAAAE8 2918A996 2F0B93B8 55F97993 EC975EEA A80D740A DBF4FF74
    7359D041 D5C33EA7 1D281E44 6B14773B CA97B43A 23FB8016 76BD207A
    436C6481 F1D2B907 8717461A 5B9D32E6 88F87748 544523B5 24B0D57D
    5EA77A27 75D2ECFA 032CFBDB F52FB378 61602790 04E57AE6 AF874E73
    03CE5329 9CCC041C 7BC308D8 2A5698F3 A8D0C382 71AE35F8 E9DBFBB6
    94B5C803 D89F7AE4 35DE236D 525F5475 9B65E372 FCD68EF2 0FA7111F
    9E4AFF73
"""

_N_4096 = """
    FFFFFFFF FFFFFFFF C90FDAA2 2168C234 C4C6628B 80DC1CD1 29024E08
    8A67CC74 020BBEA6 3B139B22 514A0879 8E3404DD EF9519B3 CD3A431B
    302B0A6D F25F1437 4FE1356D 6D51C245 E485B576 625E7EC6 F44C42E9
    A637ED6B 0BFF5CB6 F406B7ED EE386BFB 5A899FA5 AE9F2411 7C4B1FE6
    49286651 ECE45B3D C2007CB8 A163BF05 98DA4836 1C55D39A 69163FA8
    FD24CF5F 83655D23 DCA3AD96 1C62F356 208552BB 9ED52907 7096966D
    670C354E 4ABC9804 F1746C08 CA18217C 32905E46 2E36CE3B E39E772C
    180E8603 9B2783A2 EC07A28F B5C55DF0 6F4C52C9 DE2BCBF6 95581718
    3995497C EA956AE5 15D22618 98FA0510 15728E5A 8AAAC42D AD33170D
    04507A33 A85521AB DF1CBA64 ECFB8504 58DBEF0A 8AEA7157 5D060C7D
    B3970F85 A6E1E4C7 ABF5AE8C DB0933D7 1E8C94E0 4A25619D CEE3D226
    1AD2EE6B F12FFA06 D98A0864 D8760273 3EC86A64 521F2B18 177B200C
    BBE11757 7A615D6C 770988C0 BAD946E2 08E24FA0 74E5AB31 43DB5BFC
    E0FD108E 4B82D120 A9210801 1A723C12 A787E6D7 88719A10 BDBA5B26
    99C32718 6AF4E23C 1A946834 B6150BDA 2583E9CA 2AD44CE8 DBBBC2DB
    04DE8EF9 2E8EFC14 1FBECAA6 287C5947 4E6BC05D 99B2964F A090C3A2
    233BA186 515BE7ED 1F612970 CEE2D7AF B81BDD76 2170481C D0069127
    D5B05AA9 93B4EA98 8D8FDDC1 86FFB7DC 90A6C08F 4DF435C9 34063199
    FFFFFFFF FFFFFFFF
"""


@dataclass(frozen=True)
class SRPParams:
    g: int
    n: int
    hash_name: str
    n_length_bits: int
    no_user_name_in_x: bool = False


def _create_params(g: int, n_bit_length: int, hash_name: str, n_hex: str) -> SRPParams:
    return SRPParams(
        g=g,
        n=int_from_bytes(bytes_from_hex_string(n_hex)),
        hash_name=hash_name,
        n_length_bits=n_bit_length,
    )


known_groups: dict[int, SRPParams] = {
    1024: _create_params(2, 1024, "sha1", _N_1024),
    1536: _create_params(2, 1536, "sha1", _N_1536),
    2048: _create_params(2, 2048, "sha256", _N_2048),
    4096: _create_params(5, 4096, "sha256", _N_4096),
}


def get_params(n_length_bits: int) -> SRPParams:
    params = known_groups.get(n_length_bits)
    if params is None:
        raise ValueError(f"Params don't exist for {n_length_bits}")
    return params


def digest(params: SRPParams, message: bytes) -> bytes:
    return hashlib.new(params.hash_name, message).digest()


def _hash_to_int(params: SRPParams, message: bytes) -> int:
    return int_from_bytes(digest(params, message))


def calculate_a(params: SRPParams, secret: int) -> bytes:
    return pad_to_n(pow(params.g, secret, params.n), params.n_length_bits)


def calculate_u(params: SRPParams, a_value: int, b_value: int) -> int:
    return _hash_to_int(
        params,
        pad_to_n(a_value, params.n_length_bits) + pad_to_n(b_value, params.n_length_bits),
    )


def calculate_s(params: SRPParams, k: int, x: int, secret: int, b_value: int, u: int) -> bytes:
    """客户端侧 S = (B - k * g^x) ^ (a + u * x)。"""
    if b_value <= 0 or params.n <= b_value:
        raise ValueError("invalid server-supplied 'B', must be 1..N-1")
    base = (b_value - k * pow(params.g, x, params.n)) % params.n
    return pad_to_n(pow(base, secret + u * x, params.n) % params.n, params.n_length_bits)


def calculate_k(params: SRPParams, s_bytes: bytes) -> bytes:
    return digest(params, s_bytes)


def calculate_x(params: SRPParams, salt: bytes, identity: bytes, password: bytes) -> int:
    """x = H(s | H(U | ":" | p))；`no_user_name_in_x` 时省略身份部分。"""
    if params.no_user_name_in_x:
        inner = digest(params, b":" + password)
    else:
        inner = digest(params, identity + b":" + password)
    return int_from_bytes(digest(params, salt + inner))


def calculate_m1(
    params: SRPParams, username: bytes, salt: bytes, a_bytes: bytes, b_bytes: bytes, k_bytes: bytes
) -> bytes:
    """M1 按 iCloud 登录版本拼装（与 RFC 5054 不同，不要替换）。

    digest_n 哈希的是 N 本身，digest_g 哈希的是补齐到 N 字节长度的 G；对调后 M1 永远对不上。
    """
    digest_n = digest(params, int_to_bytes(params.n))
    digest_g = digest(params, pad_to_n(params.g, params.n_length_bits))
    hxor = bytes(left ^ right for left, right in zip(digest_n, digest_g, strict=True))
    return digest(
        params,
        hxor + digest(params, username) + salt + a_bytes + b_bytes + k_bytes,
    )


def calculate_m2(params: SRPParams, a_bytes: bytes, m1: bytes, k_bytes: bytes) -> bytes:
    return digest(params, a_bytes + m1 + k_bytes)


def get_multiplier(params: SRPParams) -> int:
    n_bytes = int_to_bytes(params.n)
    g_bytes = int_to_bytes(params.g)
    g_bytes = b"\x00" * (len(n_bytes) - len(g_bytes)) + g_bytes
    return _hash_to_int(params, n_bytes + g_bytes)


def compute_verifier(params: SRPParams, salt: bytes, identity: bytes, password: bytes) -> bytes:
    x = calculate_x(params, salt, identity, password)
    return pad_to_n(pow(params.g, x, params.n), params.n_length_bits)
