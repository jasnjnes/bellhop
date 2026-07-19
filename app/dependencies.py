from functools import lru_cache

from app.config import get_settings
from app.github import GitHubClient


@lru_cache
def get_github() -> GitHubClient:
    return GitHubClient(get_settings())
