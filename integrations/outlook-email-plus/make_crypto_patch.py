"""生成 OEP 本地补丁 patches/crypto.py（幂等，可重复执行）。

背景：OutlookMail Plus（OEP）v2.9.10 的账号管理接口只认「登录会话」，官方对外接口
``/api/external/*`` 只覆盖读取与号池领取。mails 后台要管理账号池，就得让**同一把对外
API Key** 既能走 ``X-API-Key``，又能当登录口令用。

做法：覆盖容器内 ``/app/outlook_web/security/crypto.py``，在 ``verify_password()`` 里追加
「对外 API Key 兜底」。全仓库只有 2 处调用 ``verify_password``（``/login`` 与导出二次验证），
所以一个文件就能覆盖，不必给 30 个账号路由逐个加装饰器。

用法（在 OEP 部署目录里执行）::

    cd /opt/outlook-email-plus
    mkdir -p patches/backup-<版本>
    docker cp outlook-email-plus:/app/outlook_web/security/crypto.py patches/backup-<版本>/crypto.py
    python3 make_crypto_patch.py            # 默认读 patches/backup-v2.9.10/crypto.py
    # docker-compose.yml 里加： - ./patches/crypto.py:/app/outlook_web/security/crypto.py:ro
    docker compose up -d

按字节改写并保留原文件行尾（CRLF/LF）；锚点必须唯一命中，否则报错退出。
"""

from __future__ import annotations

import sys
from pathlib import Path

DEFAULT_ORIG = Path("patches/backup-v2.9.10/crypto.py")
TARGET = Path("patches/crypto.py")


def build(orig_path: Path, target_path: Path) -> int:
    text = orig_path.read_text(encoding="utf-8")
    crlf = text.count("\r\n")
    total_lf = text.count("\n")
    nl = "\r\n" if crlf else "\n"
    print("原始行尾：CRLF=%d 纯LF=%d -> 使用 %s" % (crlf, total_lf - crlf, "CRLF" if crlf else "LF"))

    def L(*lines: str) -> str:
        return nl.join(lines) + nl

    if "def _machine_api_key_matches(" in text:
        print("已有补丁内容，直接复制到目标路径。")
        target_path.write_text(text, encoding="utf-8")
        return len(text)

    old_imports = L("import base64", "from typing import Optional")
    new_imports = L("import base64", "import secrets", "from typing import Optional")

    old_verify = L(
        "def verify_password(password: str, hashed: str) -> bool:",
        '    """验证密码是否匹配哈希值"""',
        "    try:",
        '        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))',
        "    except Exception:",
        "        return False",
    )

    new_verify = L(
        "def _machine_api_key_matches(password: str) -> bool:",
        '    """本地补丁（非上游代码）：对外 API Key 可作为机器调用的“密码”。',
        "",
        "    mails 后台等自动化只持有一把对外 API Key：用它走 /api/external/*（X-API-Key 头），",
        "    也用它登录会话型管理接口（/login、导出二次验证）。仅在 Key 已配置且完全匹配时返回 True。",
        '    """',
        '    provided = (password or "").strip()',
        "    if not provided:",
        "        return False",
        "    try:",
        "        from outlook_web.repositories import external_api_keys as external_api_keys_repo",
        "        from outlook_web.repositories import settings as settings_repo",
        "",
        "        configured_key = settings_repo.get_external_api_key()",
        "        if configured_key and secrets.compare_digest(provided, str(configured_key)):",
        "            return True",
        "        return bool(external_api_keys_repo.find_external_api_key_by_plaintext(provided))",
        "    except Exception:",
        "        return False",
        "",
        "",
        "def verify_password(password: str, hashed: str) -> bool:",
        '    """验证密码是否匹配哈希值（本地补丁：追加对外 API Key 兜底）"""',
        "    try:",
        '        if bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8")):',
        "            return True",
        "    except Exception:",
        "        pass",
        "    # 本地补丁新增：对外 API Key 等价于机器管理员凭据",
        "    return _machine_api_key_matches(password)",
    )

    assert text.count(old_imports) == 1, "imports 锚点不唯一，上游可能改过结构，需人工核对"
    assert text.count(old_verify) == 1, "verify_password 锚点不唯一，上游可能改过结构，需人工核对"
    out = text.replace(old_imports, new_imports).replace(old_verify, new_verify)
    target_path.write_text(out, encoding="utf-8")
    return len(out)


def main() -> None:
    orig = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ORIG
    if not orig.exists():
        raise SystemExit(f"找不到原始文件 {orig}；先 docker cp 一份出来（见文件头用法）")
    size = build(orig, TARGET)
    print("已写入 %s（%d 字节）——记得在 compose 里挂载并重建容器。" % (TARGET, size))


if __name__ == "__main__":
    main()
