"""
Application configuration loaded from environment variables via pydantic-settings.
All settings have defaults that match the docker-compose.yml dev environment.
"""

from pydantic import ConfigDict
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # MySQL
    db_host: str = "localhost"
    db_port: int = 3306
    db_name: str = "urlshortener"
    db_user: str = "root"
    db_password: str = "password"

    # Redis (Phase 2)
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_ttl_seconds: int = 3600  # Default: 1 hour TTL on cached short codes

    # Rate limiting (Phase 3) — sliding window, applied to POST /shorten
    rate_limit_requests: int = 10   # max requests allowed per window
    rate_limit_window_seconds: int = 60  # rolling window size in seconds

    # Async analytics (Phase 4) — RabbitMQ publisher + MongoDB consumer
    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"  # AMQP connection URL
    mongodb_uri: str = "mongodb://localhost:27017/urlshortener_analytics"  # includes DB name
    analytics_queue: str = "click_events"  # durable queue shared by publisher and worker

    # The public-facing base URL used when constructing short links in responses
    base_url: str = "http://localhost:8000"

    model_config = ConfigDict(env_file=".env", env_file_encoding="utf-8")


# Single shared instance imported throughout the app
settings = Settings()
