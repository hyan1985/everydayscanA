"""
Tushare Token 读取（与 ``scripts/load_tushare_token.sh`` 优先级一致）：

1. 环境变量 ``TUSHARE_TOKEN``
2. macOS 钥匙串：``security find-generic-password``，默认
   service=``cursor-quant-tushare``，account=``default``
3. 本地文件：仓库根目录下 ``.secrets/tushare_token``（建议 ``chmod 600``）

可用环境变量覆盖钥匙串与文件路径：

- ``TUSHARE_KEYCHAIN_SERVICE`` / ``TUSHARE_KEYCHAIN_ACCOUNT``
- ``TUSHARE_TOKEN_FILE``：显式指定 token 文件路径（绝对路径或相对**当前工作目录**）；不设则用仓库根下 ``.secrets/tushare_token``
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Final

# 与钥匙串中「通用密码」条目一致；可用 TUSHARE_KEYCHAIN_* 覆盖
_DEFAULT_KEYCHAIN_SERVICE: Final = "cursor-quant-tushare"
_DEFAULT_KEYCHAIN_ACCOUNT: Final = "default"

# 本文件位于 qinlong/secrets.py → 仓库根为 parent.parent
_REPO_ROOT: Final = Path(__file__).resolve().parent.parent
_DEFAULT_TOKEN_FILE: Final = _REPO_ROOT / ".secrets" / "tushare_token"


def _keychain_service() -> str:
    return os.environ.get("TUSHARE_KEYCHAIN_SERVICE", _DEFAULT_KEYCHAIN_SERVICE)


def _keychain_account() -> str:
    return os.environ.get("TUSHARE_KEYCHAIN_ACCOUNT", _DEFAULT_KEYCHAIN_ACCOUNT)


def _token_from_security_cli() -> str | None:
    service, account = _keychain_service(), _keychain_account()
    result = subprocess.run(
        [
            "security",
            "find-generic-password",
            "-s",
            service,
            "-a",
            account,
            "-w",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    token = (result.stdout or "").strip()
    return token or None


def _token_file_path() -> Path:
    override = os.environ.get("TUSHARE_TOKEN_FILE", "").strip()
    if override:
        p = Path(override)
        if not p.is_absolute():
            return (Path.cwd() / p).resolve()
        return p
    return _DEFAULT_TOKEN_FILE


def _token_from_file() -> str | None:
    path = _token_file_path()
    if not path.is_file():
        return None
    raw = path.read_text(encoding="utf-8")
    # 与 shell: tr -d '\r\n' 一致
    token = raw.replace("\r", "").replace("\n", "")
    token = token.strip()
    return token or None


def get_tushare_token() -> str:
    """
    返回 Tushare API token。

    Raises:
        RuntimeError: 未配置任何可用来源时。
    """
    env = os.environ.get("TUSHARE_TOKEN", "").strip()
    if env:
        return env

    keychain = _token_from_security_cli()
    if keychain:
        return keychain

    file_token = _token_from_file()
    if file_token:
        return file_token

    raise RuntimeError(
        "未找到 Tushare Token。请依次配置："
        "环境变量 TUSHARE_TOKEN，"
        f"或钥匙串 generic password（service={_keychain_service()!r}, account={_keychain_account()!r}），"
        f"或文件 {_token_file_path()!s}（建议 chmod 600）。"
    )
