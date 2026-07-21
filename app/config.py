"""
Application configuration loaded from environment variables via pydantic-settings.
All settings have defaults that match the docker-compose.yml dev environment.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # MySQL
    db_host: str = "localhost"
    db_port: int = 3306
    db_name: str = "urlshortener"
    db_user: str = "root"
    db_password: str = "password"

    # The public-facing base URL used when constructing short links in responses
    base_url: str = "http://localhost:8000"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


# Single shared instance imported throughout the app
settings = Settings()
