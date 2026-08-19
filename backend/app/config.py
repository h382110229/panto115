from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # 115 网盘
    cookie_115: str = ""
    app_port: int = 8000
    debug: bool = False
    http_proxy: Optional[str] = None

    # 跨盘中转: 夸克 / 阿里 / 百度
    quark_cookie: Optional[str] = None
    aliyun_refresh_token: Optional[str] = None
    baidu_bduss: Optional[str] = None
    auto_clean_temp: bool = True


settings = Settings()
