import json
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, cast  # noqa: TID251  # Prisma dynamically exposes its typed table actions

from pydantic import TypeAdapter

from litellm.models.plugin_upload import (
    PluginArchiveMetadata,
    PluginDownloader,
    PluginDownloadEvent,
    PluginDownloadsResponse,
    PluginUpload,
)
from litellm.repositories.prisma_protocols import TableActions
from litellm.repositories.table_repositories import ClaudeCodePluginRepository

if TYPE_CHECKING:
    from prisma import models as prisma_models

    from litellm.proxy.utils import PrismaClient

_MANIFEST: Final = TypeAdapter(dict[str, object])
_SOURCE: Final = TypeAdapter(dict[str, str])


class PluginUploadRepository:
    def __init__(self, prisma_client: "PrismaClient") -> None:
        self._plugins = ClaudeCodePluginRepository(prisma_client, use_writer=True).table
        self._archives = cast(  # cast-ok: Prisma exposes generated table actions through an untyped delegate
            TableActions["prisma_models.LiteLLM_ClaudeCodePluginUpload"],
            prisma_client.writer_db.litellm_claudecodepluginupload,
        )
        self._downloads = cast(  # cast-ok: Prisma exposes generated table actions through an untyped delegate
            TableActions["prisma_models.LiteLLM_ClaudeCodePluginDownload"],
            prisma_client.writer_db.litellm_claudecodeplugindownload,
        )

    async def create(self, upload: PluginUpload) -> str | None:
        from prisma import Base64
        from prisma.errors import UniqueViolationError

        try:
            plugin: Final = await self._plugins.create(
                data={
                    "name": upload.name,
                    "version": upload.version,
                    "description": upload.description,
                    "manifest_json": json.dumps(dict(upload.manifest)),
                    "created_by": upload.created_by,
                    "uploaded_archive": {"create": {"content": Base64.encode(upload.content)}},
                }
            )
        except UniqueViolationError:
            return None
        return plugin.id

    async def get_metadata(self, name: str) -> PluginArchiveMetadata | None:
        plugin: Final = await self._plugins.find_unique(where={"name": name})
        if plugin is None:
            return None
        manifest: Final = _MANIFEST.validate_json(plugin.manifest_json or "{}")
        return PluginArchiveMetadata(
            id=plugin.id,
            name=plugin.name,
            version=plugin.version,
            enabled=plugin.enabled,
            source=MappingProxyType(_SOURCE.validate_python(manifest.get("source", {}))),
            manifest_json=plugin.manifest_json,
        )

    async def get_content(self, metadata: PluginArchiveMetadata) -> bytes | None:
        archive: Final = await self._archives.find_first(
            where={
                "plugin_name": metadata.name,
                "plugin": {
                    "is": {
                        "id": metadata.id,
                        "manifest_json": metadata.manifest_json,
                        "enabled": metadata.enabled,
                    }
                },
            }
        )
        return None if archive is None else archive.content.decode()

    async def record_download(self, metadata: PluginArchiveMetadata, digest: str, downloader: PluginDownloader) -> None:
        await self._downloads.create(
            data={
                "plugin_id": metadata.id,
                "version": metadata.version,
                "digest": digest,
                "authenticated": downloader.authenticated,
                "user_id": downloader.user_id,
                "user_email": downloader.user_email,
                "key_alias": downloader.key_alias,
                "team_id": downloader.team_id,
                "team_alias": downloader.team_alias,
            }
        )

    async def downloads(self, name: str, limit: int, cursor: int | None) -> PluginDownloadsResponse | None:
        plugin: Final = await self._plugins.find_unique(where={"name": name})
        if plugin is None:
            return None
        total: Final = await self._downloads.count(where={"plugin_id": plugin.id})
        rows: Final = await self._downloads.find_many(
            where={"plugin_id": plugin.id, **({"id": {"lt": cursor}} if cursor is not None else {})},
            order={"id": "desc"},
            take=limit + 1,
        )
        return PluginDownloadsResponse(
            total_downloads=total,
            downloads=tuple(
                PluginDownloadEvent(
                    id=str(row.id),
                    downloaded_at=row.downloaded_at,
                    version=row.version,
                    digest=row.digest,
                    authenticated=row.authenticated,
                    user_id=row.user_id,
                    user_email=row.user_email,
                    key_alias=row.key_alias,
                    team_id=row.team_id,
                    team_alias=row.team_alias,
                )
                for row in rows[:limit]
            ),
            next_cursor=str(rows[limit - 1].id) if len(rows) > limit else None,
        )
