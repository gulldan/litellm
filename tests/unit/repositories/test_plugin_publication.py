import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Final, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from prisma import Base64
from prisma.models import (
    LiteLLM_ClaudeCodePluginTable,
    LiteLLM_ClaudeCodePluginUpload,
    LiteLLM_ClaudeCodePluginVersion,
)

from litellm.models.plugin_upload import (
    PluginPublication,
    PluginPublicationBase,
    PluginPublicationError,
    PublishedPlugin,
)
from litellm.repositories.plugin_publication import (
    PluginPublicationTransaction,
    publish_plugin,
    resolve_plugin_version,
    verified_archive,
)


@pytest.mark.parametrize("requested,current,updating,expected", [
    (None, None, False, "1.0.0"),
    (None, "1.0.0", True, "1.0.1"),
    (None, "9.2.99", True, "9.2.100"),
    (None, "1.2.3-rc.1+build", True, "1.2.4"),
    ("2.0.0", "1.9.9", True, "2.0.0"),
    ("1.0.0-rc.10", "1.0.0-rc.9", True, "1.0.0-rc.10"),
    ("1.0.0", "1.0.0-rc.1", True, "1.0.0"),
    ("1.0.0", None, True, "1.0.0"),
    ("2.0.0", "legacy", True, "2.0.0"),
])
def test_published_version_resolution(
    requested: str | None, current: str | None, updating: bool, expected: str,
) -> None:
    assert resolve_plugin_version(requested, current, updating=updating) == expected


@pytest.mark.parametrize("requested,current,kind", [
    ("1.0.0", "1.0.0", "invalid_version"),
    ("0.9.9", "1.0.0", "invalid_version"),
    ("1.0.0+new", "1.0.0+old", "invalid_version"),
    ("1.0.0-rc.1", "1.0.0", "invalid_version"),
    ("1.0.0-rc.9", "1.0.0-rc.10", "invalid_version"),
    ("01.2.3", "1.0.0", "invalid_version"),
    ("1.2", "1.0.0", "invalid_version"),
    ("v2.0.0", "1.0.0", "invalid_version"),
    ("2.0.0-01", "1.0.0", "invalid_version"),
    ("", "1.0.0", "invalid_version"),
    (None, None, "conflict"),
    (None, "legacy", "conflict"),
])
def test_invalid_or_non_increasing_versions_cannot_publish(
    requested: str | None, current: str | None, kind: str,
) -> None:
    result: Final = resolve_plugin_version(requested, current, updating=True)
    assert isinstance(result, PluginPublicationError)
    assert result.kind == kind
    assert result.message


@pytest.mark.parametrize("changed", [False, True])
def test_retained_archive_requires_matching_source_digest(changed: bool) -> None:
    content: Final = b"retained publication"
    manifest: Final = json.dumps({"source": {"source": "archive", "sha256": hashlib.sha256(content).hexdigest()}})
    assert verified_archive(b"different publication" if changed else content, manifest) == (None if changed else content)


class PublicationDatabase:
    def __init__(self) -> None:
        self.plugin: LiteLLM_ClaudeCodePluginTable | None = None
        self.archive: LiteLLM_ClaudeCodePluginUpload | None = None
        self.history: tuple[LiteLLM_ClaudeCodePluginVersion, ...] = ()
        self.transport = MagicMock()
        self.transport.query_raw = AsyncMock(return_value=())
        self.transport.litellm_claudecodeplugintable.find_unique = AsyncMock(side_effect=self.find_plugin)
        self.transport.litellm_claudecodeplugintable.create = AsyncMock(side_effect=self.create_plugin)
        self.transport.litellm_claudecodeplugintable.update = AsyncMock(side_effect=self.update_plugin)
        self.transport.litellm_claudecodepluginupload.find_unique = AsyncMock(side_effect=self.find_archive)
        self.transport.litellm_claudecodepluginupload.create = AsyncMock(side_effect=self.create_archive)
        self.transport.litellm_claudecodepluginupload.upsert = AsyncMock(side_effect=self.upsert_archive)
        self.transport.litellm_claudecodepluginupload.delete_many = AsyncMock(side_effect=self.delete_archive)
        self.transport.litellm_claudecodepluginversion.find_first = AsyncMock(side_effect=self.find_version)
        self.transport.litellm_claudecodepluginversion.create = AsyncMock(side_effect=self.create_version)

    @property
    def transaction(self) -> PluginPublicationTransaction:
        return cast(PluginPublicationTransaction, self.transport)

    async def find_plugin(self, *, where: Mapping[str, object]) -> LiteLLM_ClaudeCodePluginTable | None:
        return self.plugin

    async def create_plugin(self, *, data: Mapping[str, object]) -> LiteLLM_ClaudeCodePluginTable:
        self.plugin = LiteLLM_ClaudeCodePluginTable.model_validate({"id": "skill-id", "enabled": True, **data})
        return self.plugin

    async def update_plugin(
        self, *, where: Mapping[str, object], data: Mapping[str, object],
    ) -> LiteLLM_ClaudeCodePluginTable:
        assert self.plugin is not None
        self.plugin = self.plugin.model_copy(update=dict(data))
        return self.plugin

    async def find_archive(self, *, where: Mapping[str, object]) -> LiteLLM_ClaudeCodePluginUpload | None:
        return self.archive

    async def create_archive(self, *, data: Mapping[str, object]) -> LiteLLM_ClaudeCodePluginUpload:
        self.archive = LiteLLM_ClaudeCodePluginUpload.model_validate(data)
        return self.archive

    async def upsert_archive(
        self, *, where: Mapping[str, object], data: Mapping[str, Mapping[str, object]],
    ) -> LiteLLM_ClaudeCodePluginUpload:
        return await self.create_archive(data=data["create"])

    async def delete_archive(self, *, where: Mapping[str, object]) -> int:
        self.archive = None
        return 1

    async def find_version(self, *, where: Mapping[str, object]) -> LiteLLM_ClaudeCodePluginVersion | None:
        return self.history[0] if self.history else None

    async def create_version(self, *, data: Mapping[str, object]) -> LiteLLM_ClaudeCodePluginVersion:
        version: Final = LiteLLM_ClaudeCodePluginVersion.model_validate({"id": str(len(self.history)), **data})
        self.history = (*self.history, version)
        return version


def publication(
    content: bytes | None = None, *, version: str | None = None,
) -> PluginPublication:
    source: Final = (
        {"source": "archive", "sha256": hashlib.sha256(content).hexdigest()}
        if content is not None else {"source": "github", "repo": "example/skill"}
    )
    return PluginPublication("skill", version, "published metadata", {"source": source}, "admin", content)


@pytest.mark.asyncio
async def test_source_replacement_retains_the_original_uploaded_publication() -> None:
    database: Final = PublicationDatabase()
    original: Final = b"original uploaded bytes"
    created: Final = await publish_plugin(database.transaction, publication(original))
    assert isinstance(created, PublishedPlugin)
    updated: Final = await publish_plugin(
        database.transaction, publication(), PluginPublicationBase(created.id, created.updated_at),
    )
    assert isinstance(updated, PublishedPlugin)
    assert updated.version == "1.0.1"
    assert database.archive is None
    assert [row.version for row in database.history] == ["1.0.0", "1.0.1"]
    assert database.history[0].content is not None
    assert database.history[0].content.decode() == original
    assert database.history[0].archive_available is True
    assert database.history[1].content is None
    assert database.history[1].archive_available is False
    assert database.plugin is not None
    assert database.plugin.manifest_json == database.history[1].manifest_json
    assert json.loads(database.plugin.manifest_json or "{}")["version"] == updated.version


@pytest.mark.asyncio
async def test_metadata_update_retains_an_upload_only_when_its_source_digest_matches() -> None:
    database: Final = PublicationDatabase()
    original: Final = publication(b"retained archive")
    created: Final = await publish_plugin(database.transaction, original)
    assert isinstance(created, PublishedPlugin)
    metadata: Final = PluginPublication("skill", None, "new description", original.manifest, "admin")
    result: Final = await publish_plugin(
        database.transaction, metadata, PluginPublicationBase(created.id, created.updated_at),
    )
    assert isinstance(result, PublishedPlugin)
    assert database.archive is not None
    assert database.archive.content.decode() == original.content
    assert len(database.history) == 2
    assert all(row.content is not None and row.content.decode() == original.content for row in database.history)


@pytest.mark.parametrize("wrong_identity", [False, True])
@pytest.mark.asyncio
async def test_stale_publication_cannot_change_catalog_archive_or_history(wrong_identity: bool) -> None:
    database: Final = PublicationDatabase()
    created: Final = await publish_plugin(database.transaction, publication(b"original"))
    assert isinstance(created, PublishedPlugin)
    before: Final = (database.plugin, database.archive, database.history)
    expected: Final = PluginPublicationBase(
        "recreated-id" if wrong_identity else created.id,
        created.updated_at if wrong_identity else created.updated_at - timedelta(seconds=1),
    )
    result: Final = await publish_plugin(database.transaction, publication(b"replacement"), expected)
    assert isinstance(result, PluginPublicationError) and result.kind == "conflict"
    assert (database.plugin, database.archive, database.history) == before


@pytest.mark.asyncio
async def test_legacy_upload_is_snapshotted_before_its_first_semantic_publication() -> None:
    database: Final = PublicationDatabase()
    original: Final = publication(b"legacy archive")
    timestamp: Final = datetime(2025, 1, 1, tzinfo=timezone.utc)
    database.plugin = LiteLLM_ClaudeCodePluginTable(
        id="legacy", name="skill", enabled=False, version=None,
        manifest_json=json.dumps(dict(original.manifest)), updated_at=timestamp,
    )
    database.archive = LiteLLM_ClaudeCodePluginUpload(plugin_name="skill", content=Base64.encode(b"legacy archive"))
    result: Final = await publish_plugin(
        database.transaction, publication(version="1.0.0"), PluginPublicationBase("legacy", timestamp),
    )
    assert isinstance(result, PublishedPlugin) and result.enabled is False
    assert [row.version for row in database.history] == [None, "1.0.0"]
    assert database.history[0].published_at == timestamp
    assert database.history[0].content is not None and database.history[0].content.decode() == b"legacy archive"


@pytest.mark.parametrize("failure", ["duplicate", "missing", "invalid", "digest"])
@pytest.mark.asyncio
async def test_rejected_publication_leaves_all_persisted_state_unchanged(failure: str) -> None:
    database: Final = PublicationDatabase()
    initial: Final = await publish_plugin(database.transaction, publication(b"initial"))
    assert isinstance(initial, PublishedPlugin)
    if failure == "missing":
        database.plugin = None
    candidate: Final = (
        PluginPublication("skill", None, None, {"source": {"source": "archive", "sha256": "wrong"}}, None, b"bad")
        if failure == "digest" else publication(version="0.0.1" if failure == "invalid" else None)
    )
    expected: Final = None if failure == "duplicate" else PluginPublicationBase(initial.id, initial.updated_at)
    before: Final = (database.plugin, database.archive, database.history)
    result: Final = await publish_plugin(database.transaction, candidate, expected)
    assert isinstance(result, PluginPublicationError)
    assert (database.plugin, database.archive, database.history) == before
