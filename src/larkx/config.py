import os
from pathlib import Path

_home_env = os.environ.get("LARKX_HOME") or str(Path.home() / ".larkx")
DATA_DIR = Path(os.path.expanduser(_home_env))
CREDENTIALS_PATH = DATA_DIR / "credentials.json"


def load_config() -> dict:
    """保留最小配置：LarkX 只需要默认区域和凭证目录。"""
    return {"region": os.environ.get("LARKX_REGION", "feishu")}
