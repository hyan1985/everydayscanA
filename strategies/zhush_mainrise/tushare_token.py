"""
Tushare token 读取优先级：
1) 环境变量 TUSHARE_TOKEN
2) macOS「登录」钥匙串：security find-generic-password -s cursor-quant-tushare -a default -w
"""

import os
import shutil
import subprocess
import sys
from typing import Optional


def get_tushare_token() -> Optional[str]:
    """返回可用的 token；未配置则返回 None。"""
    env = os.environ.get("TUSHARE_TOKEN", "").strip()
    if env:
        return env

    if sys.platform == "darwin":
        security = shutil.which("security")
        if security:
            try:
                proc = subprocess.run(
                    [
                        security,
                        "find-generic-password",
                        "-s",
                        "cursor-quant-tushare",
                        "-a",
                        "default",
                        "-w",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessTimeoutExpired):
                proc = None
            if proc is not None and proc.returncode == 0 and proc.stdout:
                keychain_token = "".join(c for c in proc.stdout if c not in "\r\n").strip()
                if keychain_token:
                    return keychain_token

    return None
