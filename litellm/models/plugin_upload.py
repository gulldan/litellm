from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol


@dataclass(frozen=True, slots=True)
class PluginUpload:
    name: str
    version: str | None
    description: str | None
    manifest: Mapping[str, object]
    content: bytes
    created_by: str | None


@dataclass(frozen=True, slots=True)
class PluginArchiveMetadata:
    id: str
    name: str
    enabled: bool
    source: Mapping[str, str]
    manifest_json: str | None


@dataclass(frozen=True, slots=True)
class PluginPublication:
    name: str
    version: str | None
    description: str | None
    manifest: Mapping[str, object]
    created_by: str | None
    content: bytes | None = None


@dataclass(frozen=True, slots=True)
class PluginPublicationBase:
    id: str
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class PublishedPlugin:
    id: str
    name: str
    version: str
    enabled: bool
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PluginPublicationError:
    kind: Literal["invalid_version", "conflict", "not_found"]
    message: str


@dataclass(frozen=True, slots=True)
class PluginVersion:
    id: str
    version: str | None
    description: str | None
    manifest: Mapping[str, object]
    published_at: datetime | None
    published_by: str | None
    archive_available: bool


@dataclass(frozen=True, slots=True)
class PluginSnapshot:
    id: str
    name: str
    version: str | None
    description: str | None
    manifest: Mapping[str, object]
    enabled: bool
    created_at: datetime | None
    updated_at: datetime | None
    created_by: str | None


class PluginUploadStore(Protocol):
    async def create(self, upload: PluginUpload) -> str | None: ...

    async def get_plugin(self, name: str) -> PluginSnapshot | None: ...

    async def publish(
        self, publication: PluginPublication, expected: PluginPublicationBase | None = None
    ) -> PublishedPlugin | PluginPublicationError: ...

    async def versions(self, metadata: PluginArchiveMetadata, limit: int, offset: int) -> tuple[PluginVersion, ...]: ...

    async def version_content(
        self, plugin_id: str, version_id: str, metadata: PluginArchiveMetadata
    ) -> bytes | None: ...

    async def get_metadata(self, name: str) -> PluginArchiveMetadata | None: ...

    async def get_content(self, metadata: PluginArchiveMetadata) -> bytes | None: ...
