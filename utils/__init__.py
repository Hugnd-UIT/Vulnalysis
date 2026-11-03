import importlib

policy = importlib.import_module(".bypass-policy", package=__name__)
apply = policy.apply
SAFETY_SETTINGS = policy.SAFETY_SETTINGS

from .logger import setup_logger
from .skip import is_non_prod, NON_PROD

__all__ = ["policy", "apply", "SAFETY_SETTINGS", "setup_logger", "is_non_prod", "NON_PROD"]
