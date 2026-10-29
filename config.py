from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    BOT_TOKEN: str
    API_ID: int
    API_HASH: str

    # Где хранить БД (на Render это /data — диск)
    DATA_DIR: str = "/data"

    # Через запятую. Пусто = ботом может пользоваться кто угодно.
    # Настоятельно рекомендую вписать свой telegram id.
    ALLOWED_USER_IDS: str = ""

    # Максимум аккаунтов на одного пользователя
    MAX_ACCOUNTS: int = 10

    @property
    def allowed_ids(self) -> set[int]:
        return {
            int(x.strip())
            for x in self.ALLOWED_USER_IDS.split(",")
            if x.strip().isdigit()
        }


settings = Settings()
