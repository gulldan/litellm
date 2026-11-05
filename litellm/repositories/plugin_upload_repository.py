from contextlib import AbstractAsyncContextManager
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Final,
    Protocol,
    cast,  # noqa: TID251  # Prisma dynamically exposes its typed table actions
)

from pydantic import TypeAdapter

from litellm.models.plugin_upload import (
    PluginArchiveMetadata,
    PluginPublication,
    PluginPublicationBase,
    PluginPublicationError,
    PluginUpload,
    PluginVersion,
    PublishedPlugin,
)
from litellm.repositories.plugin_publication import PluginPublicationTransaction, publish_plugin, verified_archive
from litellm.repositories.prisma_protocols import TableActions
from litellm.repositories.table_repositories import ClaudeCodePluginRepository

if TYPE_CHECKING:
    from prisma import models as prisma_models

    from litellm.proxy.utils import PrismaClient

_MANIFEST: Final = TypeAdapter(dict[str, object])
_SOURCE: Final = TypeAdapter(dict[str, str])
_VERSIONS: Final = TypeAdapter(tuple[PluginVersion, ...])


class _PublicationDatabase(PluginPublicationTransaction, Protocol):
    def tx(self) -> AbstractAsyncContextManager[PluginPublicationTransaction]: ...


class PluginUploadRepository:
    def __init__(self, prisma_client: "PrismaClient") -> None:
        self._db = cast(  # cast-ok: the generated Prisma transaction delegate is untyped at this boundary
            _PublicationDatabase, prisma_client.writer_db
        )
        self._plugins = ClaudeCodePluginRepository(prisma_client, use_writer=True).table
        self._archives = cast(  # cast-ok: Prisma exposes generated table actions through an untyped delegate
            TableActions["prisma_models.LiteLLM_ClaudeCodePluginUpload"],
            prisma_client.writer_db.litellm_claudecodepluginupload,
        )

    async def create(self, upload: PluginUpload) -> str | None:
        result: Final = await self.publish(
            PluginPublication(
                name=upload.name,
                version=upload.version,
                description=upload.description,
                manifest=upload.manifest,
                content=upload.content,
                created_by=upload.created_by,
            )
        )
        return result.id if isinstance(result, PublishedPlugin) else None

    async def publish(
        self, publication: PluginPublication, expected: PluginPublicationBase | None = None
    ) -> PublishedPlugin | PluginPublicationError:
        from prisma.errors import UniqueViolationError

        try:
            async with self._db.tx() as tx:
                return await publish_plugin(tx, publication, expected)
        except UniqueViolationError:
            return PluginPublicationError("conflict", "A skill with this name or version already exists")

    async def versions(self, metadata: PluginArchiveMetadata, limit: int, offset: int) -> tuple[PluginVersion, ...]:
        rows: Final = await self._db.query_raw(
            """SELECT v.id, v.version, v.description, COALESCE(v.manifest_json, '{}')::jsonb AS manifest,
                      v.published_at, v.published_by, v.archive_available
               FROM "LiteLLM_ClaudeCodePluginVersion" v JOIN "LiteLLM_ClaudeCodePluginTable" p ON p.id = v.plugin_id
               WHERE p.id = $1 AND p.manifest_json IS NOT DISTINCT FROM $2::text AND p.enabled = $3
               ORDER BY v.published_at DESC NULLS LAST, v.id DESC LIMIT $4 OFFSET $5""",
            metadata.id,
            metadata.manifest_json,
            metadata.enabled,
            limit,
            offset,
        )
        return _VERSIONS.validate_python(rows)

    async def version_content(self, plugin_id: str, version_id: str, metadata: PluginArchiveMetadata) -> bytes | None:
        version: Final = await self._db.litellm_claudecodepluginversion.find_first(
            where={
                "id": version_id,
                "plugin_id": plugin_id,
                "plugin": {
                    "is": {
                        "manifest_json": metadata.manifest_json,
                        "enabled": metadata.enabled,
                    }
                },
            }
        )
        if version is None or version.content is None:
            return None
        return verified_archive(version.content.decode(), version.manifest_json)

    async def get_metadata(self, name: str) -> PluginArchiveMetadata | None:
        plugin: Final = await self._plugins.find_unique(where={"name": name})
        if plugin is None:
            return None
        manifest: Final = _MANIFEST.validate_json(plugin.manifest_json or "{}")
        return PluginArchiveMetadata(
            id=plugin.id,
            name=plugin.name,
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
                        "manifest_json": metadata.manifest_json,
                        "enabled": metadata.enabled,
                    }
                },
            }
        )
        return None if archive is None else archive.content.decode()
