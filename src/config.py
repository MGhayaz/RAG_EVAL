from dotenv import load_dotenv
load_dotenv()
from pydantic import Field
from pydantic_settings import BaseSettings,SettingsConfigDict
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    GEMINI_API_KEY: str = Field(min_length=1)
    DB_URL: str = Field(min_length=1)
    MODEL_NAME: str = Field(min_length=1)
settings = Settings()    