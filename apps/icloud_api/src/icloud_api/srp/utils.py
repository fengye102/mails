"""字节/整数工具（对应 CYMail `internal/srp/utils.go`）。

行为差异只允许一处：Go 的 `padTo` 在目标长度小于输入长度时会 panic（`make` 负长度），
这里改成抛 `ValueError`，语义一致（都是"不允许"）。
"""

import re

_HEX_ONLY = re.compile(r"[^0-9a-fA-F]")


def pad_to(value: bytes, length: int) -> bytes:
    padding_length = length - len(value)
    if padding_length < 0:
        raise ValueError("value is longer than the padded length")
    return b"\x00" * padding_length + value


def pad_to_n(number: int, n_length_bits: int) -> bytes:
    return pad_to(int_to_bytes(number), n_length_bits // 8)


def int_from_bytes(value: bytes) -> int:
    return int.from_bytes(value, "big") if value else 0


def int_to_bytes(value: int) -> bytes:
    if value == 0:
        return b""
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


def bytes_from_hex_string(value: str) -> bytes:
    return bytes.fromhex(_HEX_ONLY.sub("", value))
