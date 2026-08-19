from ..exception_handling import ErrorKind, classify, with_retries
from .base import ProviderClient
from .client import AgentEvent, AgentRunResult, DeepSeekClient


def make_client(config) -> ProviderClient:
    """Construct the provider adapter selected by ``config``.

    Only DeepSeek is wired today; the factory exists so callers already reach
    the pipeline through a swap point. When a second adapter lands, the branch
    is here, not sprinkled through the pipeline.
    """
    return DeepSeekClient(config)


__all__ = [
    "AgentEvent",
    "AgentRunResult",
    "DeepSeekClient",
    "ErrorKind",
    "ProviderClient",
    "classify",
    "make_client",
    "with_retries",
]
