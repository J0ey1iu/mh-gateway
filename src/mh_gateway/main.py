"""开箱即用入口。

直接 ``uvicorn mh_gateway.main:app`` 即可启动。
配置从环境变量（或 ``.env`` 文件）读取。
"""

import asyncio

from mh_gateway.app import create_app
from mh_gateway.config import ConfigSchema
from mh_gateway.config_manager import ConfigManager

_config_mgr = ConfigManager()
_settings = asyncio.run(_config_mgr.resolve(ConfigSchema, prefix="ORCH"))
app = create_app(settings=_settings)
