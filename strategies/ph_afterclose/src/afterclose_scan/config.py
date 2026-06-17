from __future__ import annotations

import getpass
import os
import subprocess
from pathlib import Path


def _read_keychain_password(service: str, account: str) -> str | None:
    cmd = [
        "security",
        "find-generic-password",
        "-s",
        service,
        "-a",
        account,
        "-w",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    secret = result.stdout.strip()
    return secret or None


def get_tushare_token() -> str:
    token = os.getenv("TUSHARE_TOKEN")
    if token:
        return token.strip()

    service = os.getenv("TUSHARE_KEYCHAIN_SERVICE", "cursor-quant-tushare")
    account = os.getenv("TUSHARE_KEYCHAIN_ACCOUNT", "default")
    from_keychain = _read_keychain_password(service=service, account=account)
    if from_keychain:
        return from_keychain

    secret_file = Path(os.getenv("TUSHARE_TOKEN_FILE", ".secrets/tushare_token"))
    if secret_file.exists():
        file_token = secret_file.read_text(encoding="utf-8").strip()
        if file_token:
            return file_token

    raise RuntimeError(
        "未找到 Tushare Token。请设置环境变量 TUSHARE_TOKEN，"
        "或将 token 存入 Apple Keychain（service=cursor-quant-tushare, account=default），"
        "或写入 .secrets/tushare_token。"
    )

