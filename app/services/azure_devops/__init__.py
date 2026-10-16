from __future__ import annotations

from functools import lru_cache

from app.config import Settings, get_settings
from app.services.azure_devops.base import WorkItemProvider
from app.services.azure_devops.demo import DemoProvider
from app.services.azure_devops.mapping import WorkItemMapping


def build_provider(settings: Settings | None = None) -> WorkItemProvider:
    settings = settings or get_settings()
    mapping = WorkItemMapping.load(settings.work_item_mapping_path)
    if settings.demo_mode:
        return DemoProvider()
    from app.services.azure_devops.real import AzureDevOpsProvider

    return AzureDevOpsProvider(settings, mapping)


@lru_cache(maxsize=1)
def get_provider() -> WorkItemProvider:
    return build_provider()


def get_mapping() -> WorkItemMapping:
    return WorkItemMapping.load(get_settings().work_item_mapping_path)


def reset_provider_cache() -> None:
    get_provider.cache_clear()
