from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


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
    version: str | None
    enabled: bool
    source: Mapping[str, str]
    manifest_json: str | None


@dataclass(frozen=True, slots=True)
class PluginDownloader:
    authenticated: bool
    user_id: str | None
    user_email: str | None
    key_alias: str | None
    team_id: str | None
    team_alias: str | None


@dataclass(frozen=True, slots=True)
class PluginDownloadEvent:
    id: str
    downloaded_at: datetime
    version: str | None
    digest: str
    authenticated: bool
    user_id: str | None
    user_email: str | None
    key_alias: str | None
    team_id: str | None
    team_alias: str | None


@dataclass(frozen=True, slots=True)
class PluginDownloadsResponse:
    total_downloads: int
    downloads: tuple[PluginDownloadEvent, ...]
    next_cursor: str | None


class PluginUploadStore(Protocol):
    async def create(self, upload: PluginUpload) -> str | None: ...

    async def get_metadata(self, name: str) -> PluginArchiveMetadata | None: ...

    async def get_content(self, metadata: PluginArchiveMetadata) -> bytes | None: ...

    async def record_download(
        self, metadata: PluginArchiveMetadata, digest: str, downloader: PluginDownloader
    ) -> None: ...

    async def downloads(self, name: str, limit: int, cursor: int | None) -> PluginDownloadsResponse | None: ...
