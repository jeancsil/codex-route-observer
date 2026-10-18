import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    db_path: Path = field(
        default_factory=lambda: Path(os.getenv("OBSERVER_DB", "data/observer.sqlite3"))
    )
    policy_path: Path = field(
        default_factory=lambda: Path(os.getenv("OBSERVER_POLICY", "policy.toml"))
    )
    api_key: str = field(default_factory=lambda: os.getenv("TYPESAFE_API_KEY", ""), repr=False)
    typesafe_base: str = field(
        default_factory=lambda: os.getenv("TYPESAFE_API_BASE", "https://api.typesafe.ai").rstrip(
            "/"
        )
    )
    typesafe_model: str = field(default_factory=lambda: os.getenv("TYPESAFE_MODEL", "jev-latest"))
    jev_timeout: float = field(
        default_factory=lambda: float(os.getenv("OBSERVER_JEV_TIMEOUT", "10"))
    )
    concurrency: int = field(
        default_factory=lambda: int(os.getenv("OBSERVER_CLASSIFICATION_CONCURRENCY", "2"))
    )
    queue_size: int = 256
    chatgpt_upstream: str = "https://chatgpt.com/backend-api/codex"
    api_upstream: str = "https://api.openai.com/v1"

    def __post_init__(self):
        if self.concurrency < 1 or self.jev_timeout <= 0:
            raise ValueError("Classification concurrency and timeout must be positive")


def load_policy(path: Path) -> dict:
    with path.open("rb") as source:
        policy = tomllib.load(source)
    if not 2 <= len(policy.get("models", {})) <= 20:
        raise ValueError("Policy needs 2–20 model candidates")
    for model, candidate in policy["models"].items():
        if not model or not candidate.get("criteria"):
            raise ValueError("Every candidate needs a model ID and criteria")
    threshold = policy.get("minimum_confidence", 0.6)
    if not 0 <= threshold <= 1:
        raise ValueError("minimum_confidence must be between 0 and 1")
    return policy
