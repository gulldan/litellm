"""
Unit tests for claude_code_marketplace.py source validation.

Covers the git-subdir and archive source types added alongside the existing github and url types.
"""

import hashlib
import io
import json
import zipfile
from collections.abc import AsyncIterator, Mapping
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Final, TypeAlias
from unittest.mock import AsyncMock, MagicMock, call

import httpx
import pytest
from fastapi import HTTPException
from prisma import Base64
from prisma import models as prisma_models
from prisma.errors import UniqueViolationError
from pydantic import ValidationError

import litellm
from litellm.caching.caching import DualCache
from litellm.proxy._types import CommonProxyErrors, LiteLLM_ObjectPermissionTable, ProxyException, UserAPIKeyAuth
from litellm.proxy.anthropic_endpoints.claude_code_endpoints import claude_code_marketplace
from litellm.proxy.anthropic_endpoints.claude_code_endpoints.claude_code_marketplace import (
    delete_plugin,
    disable_plugin,
    enable_plugin,
    get_marketplace,
    get_plugin,
    list_plugins,
)
from litellm.proxy.anthropic_endpoints.claude_code_endpoints.claude_code_marketplace import (
    register_plugin as register_plugin_route,
)
from litellm.proxy.anthropic_endpoints.claude_code_endpoints.claude_code_marketplace import (
    update_plugin as update_plugin_route,
)
from litellm.proxy.deps.repositories import get_plugin_upload_repository, get_skill_submission_repository
from litellm.proxy.proxy_server import LitellmUserRoles, hash_token
from litellm.repositories.plugin_upload_repository import PluginUploadRepository
from litellm.repositories.skill_submission_repository import SkillSubmissionRepository
from litellm.types.proxy.claude_code_endpoints import (
    MarketplaceResponse,
    RegisterPluginRequest,
    RegisterPluginResponse,
    UpdatePluginRequest,
)
from tests._master_key import MASTER_KEY


def _make_mock_prisma():
    """Stateful prisma mock that supports find_unique, find_many, create, and update."""
    store: dict = {}

    mock_client = MagicMock()
    mock_client.proxy_logging_obj = MagicMock()
    mock_table = MagicMock()

    async def _find_unique(where):
        return store.get(where.get("name"))

    def _matches(record, where) -> bool:
        if "OR" in where:
            return any(_matches(record, clause) for clause in where["OR"])
        if "enabled" in where and record.enabled != where["enabled"]:
            return False
        return "name" not in where or record.name in where["name"]["in"]

    async def _find_many(where=None):
        return [r for r in store.values() if _matches(r, where or {})]

    async def _create(data):
        record = MagicMock()
        record.id = data["name"]
        record.name = data["name"]
        record.version = data.get("version")
        record.description = data.get("description")
        record.manifest_json = data.get("manifest_json", "{}")
        record.enabled = data.get("enabled", True)
        record.created_at = data.get("created_at", datetime(2026, 1, 1, tzinfo=timezone.utc))
        record.created_by = data.get("created_by")
        record.updated_at = data.get("updated_at", datetime(2026, 1, 1, tzinfo=timezone.utc))
        store[data["name"]] = record
        return record

    async def _update(where, data):
        record = store[where["name"]] if "name" in where else next(r for r in store.values() if r.id == where["id"])
        for k, v in data.items():
            setattr(record, k, v)
        return record

    mock_table.find_unique = AsyncMock(side_effect=_find_unique)
    mock_table.find_many = AsyncMock(side_effect=_find_many)
    mock_table.create = AsyncMock(side_effect=_create)
    mock_table.update = AsyncMock(side_effect=_update)
    mock_client.db.litellm_claudecodeplugintable = mock_table
    mock_client.writer_db = mock_client.db
    mock_client.writer_db.query_raw = AsyncMock(return_value=[])
    mock_client.writer_db.tx.return_value.__aenter__.return_value = mock_client.writer_db
    mock_client.writer_db.litellm_claudecodepluginversion.find_first = AsyncMock(return_value=None)
    mock_client.writer_db.litellm_claudecodepluginversion.create = AsyncMock()
    mock_client.writer_db.litellm_claudecodepluginupload.find_unique = AsyncMock(return_value=None)
    mock_client.writer_db.litellm_claudecodepluginupload.upsert = AsyncMock()
    mock_client.writer_db.litellm_claudecodepluginupload.create = AsyncMock()
    mock_client.writer_db.litellm_claudecodepluginupload.delete_many = AsyncMock(return_value=0)
    mock_client.writer_db.litellm_skillsubmission.create = AsyncMock(
        side_effect=lambda data: prisma_models.LiteLLM_SkillSubmission(
            id="submission-id",
            status="pending_review",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            **data,
        )
    )
    return mock_client


async def register_plugin(request: RegisterPluginRequest, user_api_key_dict: UserAPIKeyAuth) -> RegisterPluginResponse:
    prisma: Final = litellm.proxy.proxy_server.prisma_client
    return await register_plugin_route(
        request=request,
        user_api_key_dict=user_api_key_dict,
        uploads=PluginUploadRepository(prisma),
        submissions=SkillSubmissionRepository(prisma),
    )


async def update_plugin(
    plugin_name: str,
    request: UpdatePluginRequest,
    user_api_key_dict: UserAPIKeyAuth,
) -> RegisterPluginResponse:
    prisma: Final = litellm.proxy.proxy_server.prisma_client
    return await update_plugin_route(
        plugin_name=plugin_name,
        request=request,
        user_api_key_dict=user_api_key_dict,
        uploads=PluginUploadRepository(prisma),
        submissions=SkillSubmissionRepository(prisma),
    )


_USER = UserAPIKeyAuth(
    user_role=LitellmUserRoles.PROXY_ADMIN,
    api_key=MASTER_KEY,
    user_id="test-user",
)

_NON_ADMIN_USER = UserAPIKeyAuth(
    user_role=LitellmUserRoles.INTERNAL_USER,
    api_key="sk-5678",
    user_id="regular-user",
)

_GIT_SUBDIR_SOURCE = {
    "source": "git-subdir",
    "url": "https://github.com/org/monorepo.git",
    "path": "plugins/my-plugin",
}

_ARCHIVE_SOURCE = {
    "source": "archive",
    "url": "https://skills-bucket.s3.us-east-1.amazonaws.com/plugins/s3-skill-1.0.0.zip",
    "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
}


@pytest.fixture(autouse=True)
def _patch_proxy_globals(monkeypatch):
    """Scope prisma_client/master_key mutations to each test via monkeypatch."""
    monkeypatch.setattr(litellm.proxy.proxy_server, "prisma_client", _make_mock_prisma())
    monkeypatch.setattr(litellm.proxy.proxy_server, "master_key", MASTER_KEY)


@pytest.mark.asyncio
async def test_register_plugin_git_subdir_success():
    """git-subdir with both url and path fields registers successfully."""
    request = RegisterPluginRequest(name="my-monorepo-plugin", source=_GIT_SUBDIR_SOURCE)

    response = await register_plugin(request=request, user_api_key_dict=_USER)

    assert response.status == "success"
    assert response.action == "created"
    assert response.plugin.source["source"] == "git-subdir"
    assert response.plugin.source["path"] == "plugins/my-plugin"


async def _read_stored_manifest(name: str) -> dict:
    table = litellm.proxy.proxy_server.prisma_client.db.litellm_claudecodeplugintable
    record = await table.find_unique(where={"name": name})
    return json.loads(record.manifest_json)


@pytest.mark.asyncio
async def test_register_plugin_duplicate_name_conflicts():
    """A second POST with an existing name returns 409 and leaves the stored plugin untouched."""
    name = "my-monorepo-plugin"
    await register_plugin(
        request=RegisterPluginRequest(name=name, source=_GIT_SUBDIR_SOURCE, version="1.0.0"),
        user_api_key_dict=_USER,
    )

    stored_before = await _read_stored_manifest(name)
    assert stored_before["version"] == "1.0.0"

    conflicting = RegisterPluginRequest(
        name=name,
        source={
            "source": "git-subdir",
            "url": "https://github.com/org/other.git",
            "path": "plugins/other-plugin",
        },
        version="2.0.0",
    )
    with pytest.raises(HTTPException) as exc_info:
        await register_plugin(request=conflicting, user_api_key_dict=_USER)

    assert exc_info.value.status_code == 409
    assert "already exists" in exc_info.value.detail["error"]

    stored_after = await _read_stored_manifest(name)
    assert stored_after == stored_before
    assert stored_after["version"] == "1.0.0"
    assert stored_after["source"]["url"] == "https://github.com/org/monorepo.git"


@pytest.mark.asyncio
async def test_update_plugin_replaces_existing_source():
    """PUT updates an existing plugin: action=updated and the stored source is replaced."""
    name = "my-monorepo-plugin"
    await register_plugin(
        request=RegisterPluginRequest(name=name, source=_GIT_SUBDIR_SOURCE, version="1.0.0"),
        user_api_key_dict=_USER,
    )

    new_source = {"source": "github", "repo": "org/replacement"}
    response = await update_plugin(
        plugin_name=name,
        request=UpdatePluginRequest(source=new_source, version="2.0.0", description="updated"),
        user_api_key_dict=_USER,
    )

    assert response.status == "success"
    assert response.action == "updated"
    assert response.plugin.version == "2.0.0"
    assert response.plugin.source == new_source

    stored = await _read_stored_manifest(name)
    assert stored["source"] == new_source
    assert stored["version"] == "2.0.0"


@pytest.mark.asyncio
async def test_metadata_edits_preserve_uploaded_source_and_reject_a_changed_http_destination() -> None:
    table: Final = litellm.proxy.proxy_server.prisma_client.writer_db.litellm_claudecodeplugintable
    await table.create(data={"name": _UPLOAD_NAME, "manifest_json": json.dumps({"source": _UPLOAD_SOURCE})})
    request: Final = UpdatePluginRequest(source=_UPLOAD_SOURCE, description="Updated description", version="1.0.0")
    response: Final = await update_plugin(plugin_name=_UPLOAD_NAME, request=request, user_api_key_dict=_USER)
    expected: Final = {"name": _UPLOAD_NAME, "source": _UPLOAD_SOURCE, "description": "Updated description", "version": "1.0.0"}
    assert response.plugin.description == "Updated description"
    assert await _read_stored_manifest(_UPLOAD_NAME) == expected
    with pytest.raises(HTTPException) as error:
        await update_plugin(
            plugin_name=_UPLOAD_NAME, user_api_key_dict=_USER,
            request=UpdatePluginRequest(source={**_UPLOAD_SOURCE, "url": "http://other.example/skill.zip"}),
        )
    assert error.value.status_code == 400
    table.update.assert_awaited_once()
    assert await _read_stored_manifest(_UPLOAD_NAME) == expected


@pytest.mark.asyncio
async def test_update_plugin_not_found():
    """PUT on a name that does not exist raises HTTP 404."""
    with pytest.raises(HTTPException) as exc_info:
        await update_plugin(
            plugin_name="does-not-exist",
            request=UpdatePluginRequest(source=_GIT_SUBDIR_SOURCE),
            user_api_key_dict=_USER,
        )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_register_plugin_create_race_maps_unique_violation_to_409():
    """A concurrent insert that slips past the find_unique pre-check (create raises
    the unique-constraint error) is mapped to 409, not surfaced as a 500."""
    from prisma.errors import UniqueViolationError

    table = litellm.proxy.proxy_server.prisma_client.db.litellm_claudecodeplugintable
    table.create = AsyncMock(side_effect=UniqueViolationError({}, message="duplicate name"))

    with pytest.raises(HTTPException) as exc_info:
        await register_plugin(
            request=RegisterPluginRequest(name="racy-plugin", source=_GIT_SUBDIR_SOURCE),
            user_api_key_dict=_USER,
        )

    assert exc_info.value.status_code == 409
    assert "already exists" in exc_info.value.detail["error"]


@pytest.mark.asyncio
async def test_update_plugin_db_error_maps_to_structured_500():
    """A data-layer failure during the update (e.g. a dropped DB connection) is caught and
    returned as a structured 500, not swallowed silently or leaked as an unhandled error."""
    from prisma.errors import PrismaError

    name = "my-monorepo-plugin"
    await register_plugin(
        request=RegisterPluginRequest(name=name, source=_GIT_SUBDIR_SOURCE, version="1.0.0"),
        user_api_key_dict=_USER,
    )

    table = litellm.proxy.proxy_server.prisma_client.db.litellm_claudecodeplugintable
    table.update = AsyncMock(side_effect=PrismaError("connection lost"))

    with pytest.raises(HTTPException) as exc_info:
        await update_plugin(
            plugin_name=name,
            request=UpdatePluginRequest(source={"source": "github", "repo": "org/replacement"}),
            user_api_key_dict=_USER,
        )

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == {"error": "Skill write failed"}


@pytest.mark.asyncio
async def test_update_plugin_deleted_mid_update_returns_404():
    """A concurrent delete between the find_unique pre-check and the update makes prisma's
    update return None; that must surface the same 404 as a plain miss, not an AttributeError."""
    name = "my-monorepo-plugin"
    await register_plugin(
        request=RegisterPluginRequest(name=name, source=_GIT_SUBDIR_SOURCE, version="1.0.0"),
        user_api_key_dict=_USER,
    )

    table = litellm.proxy.proxy_server.prisma_client.db.litellm_claudecodeplugintable
    existing: Final = await table.find_unique(where={"name": name})
    table.find_unique = AsyncMock(side_effect=[existing, None])

    with pytest.raises(HTTPException) as exc_info:
        await update_plugin(
            plugin_name=name,
            request=UpdatePluginRequest(source={"source": "github", "repo": "org/replacement"}),
            user_api_key_dict=_USER,
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == {"error": f"Plugin '{name}' not found"}


@pytest.mark.asyncio
async def test_get_marketplace_skips_plugin_with_null_manifest():
    await register_plugin(
        request=RegisterPluginRequest(name="good-plugin", source=_GIT_SUBDIR_SOURCE, version="1.0.0"),
        user_api_key_dict=_USER,
    )

    table = litellm.proxy.proxy_server.prisma_client.db.litellm_claudecodeplugintable
    await table.create(data={"name": "null-manifest-plugin", "manifest_json": None, "enabled": True})

    response = await get_marketplace(request=MagicMock())

    assert response.status_code == 200
    body = json.loads(response.body)
    assert [plugin["name"] for plugin in body["plugins"]] == ["good-plugin"]


def _granted_user(skills: list[str]) -> UserAPIKeyAuth:
    return UserAPIKeyAuth(
        user_role=LitellmUserRoles.INTERNAL_USER,
        api_key="sk-granted",
        user_id="granted-user",
        object_permission=LiteLLM_ObjectPermissionTable(object_permission_id="perm-1", skills=skills),
    )


async def _register_public_and_private_plugins() -> None:
    for name, enabled in (("public-skill", True), ("private-skill", False)):
        await register_plugin(
            request=RegisterPluginRequest(name=name, source=_GIT_SUBDIR_SOURCE, version="1.0.0"),
            user_api_key_dict=_USER,
        )
        if not enabled:
            await disable_plugin(plugin_name=name, user_api_key_dict=_USER)


async def _listed_names(user: UserAPIKeyAuth) -> set[str]:
    response = await list_plugins(user_api_key_dict=user)
    return {plugin.name for plugin in response.plugins}


@pytest.mark.asyncio
async def test_list_plugins_shows_disabled_plugin_only_to_granted_key_or_admin():
    await _register_public_and_private_plugins()

    assert await _listed_names(_NON_ADMIN_USER) == {"public-skill"}
    assert await _listed_names(_granted_user(["other-skill"])) == {"public-skill"}
    assert await _listed_names(_granted_user(["private-skill"])) == {"public-skill", "private-skill"}
    assert await _listed_names(_USER) == {"public-skill", "private-skill"}


@pytest.mark.asyncio
async def test_get_plugin_returns_403_for_disabled_plugin_the_key_is_not_granted():
    await _register_public_and_private_plugins()

    with pytest.raises(HTTPException) as exc_info:
        await get_plugin(plugin_name="private-skill", user_api_key_dict=_NON_ADMIN_USER)
    assert exc_info.value.status_code == 403

    assert (await get_plugin(plugin_name="public-skill", user_api_key_dict=_NON_ADMIN_USER))["name"] == "public-skill"
    granted = await get_plugin(plugin_name="private-skill", user_api_key_dict=_granted_user(["private-skill"]))
    assert granted["name"] == "private-skill"
    assert granted["enabled"] is False


async def _marketplace_names(key: str | None) -> list[str]:
    response = await get_marketplace(request=MagicMock(), key=key)
    assert response.status_code == 200
    return sorted(plugin["name"] for plugin in json.loads(response.body)["plugins"])


@pytest.mark.asyncio
async def test_get_marketplace_key_query_param_adds_granted_disabled_plugins(monkeypatch):
    await _register_public_and_private_plugins()
    keys = {"sk-granted": _granted_user(["private-skill"]), "sk-plain": _NON_ADMIN_USER}

    async def _fake_auth(request, api_key: str) -> UserAPIKeyAuth:
        token = api_key.removeprefix("Bearer ")
        if token not in keys:
            raise ProxyException(message="invalid key", type="auth_error", param="key", code=401)
        return keys[token]

    monkeypatch.setattr(claude_code_marketplace, "user_api_key_auth", _fake_auth)

    assert await _marketplace_names(None) == ["public-skill"]
    assert await _marketplace_names("sk-plain") == ["public-skill"]
    assert await _marketplace_names("sk-granted") == ["private-skill", "public-skill"]

    with pytest.raises(ProxyException) as exc_info:
        await get_marketplace(request=MagicMock(), key="sk-bogus")
    assert exc_info.value.code == "401"


@pytest.mark.asyncio
@pytest.mark.parametrize("preference", ["available", "auto_install", "required"])
async def test_get_marketplace_emits_installation_preference(preference):
    await register_plugin(
        request=RegisterPluginRequest(
            name="s3-skill",
            source=_ARCHIVE_SOURCE,
            installation_preference=preference,
        ),
        user_api_key_dict=_USER,
    )

    marketplace = json.loads((await get_marketplace(request=MagicMock())).body)

    assert marketplace["plugins"] == [
        {"name": "s3-skill", "source": _ARCHIVE_SOURCE, "version": "1.0.0", "installationPreference": preference}
    ]


@pytest.mark.parametrize(
    "build_request",
    [
        lambda preference: RegisterPluginRequest(
            name="s3-skill", source=_ARCHIVE_SOURCE, installation_preference=preference
        ),
        lambda preference: UpdatePluginRequest(source=_ARCHIVE_SOURCE, installation_preference=preference),
    ],
    ids=["register", "update"],
)
def test_plugin_request_rejects_unknown_installation_preference(build_request):
    with pytest.raises(ValidationError, match="installation_preference"):
        build_request("auto-install")


@pytest.mark.asyncio
async def test_get_marketplace_omits_installation_preference_when_unset():
    await register_plugin(
        request=RegisterPluginRequest(name="manual-install-plugin", source=_GIT_SUBDIR_SOURCE),
        user_api_key_dict=_USER,
    )

    marketplace = json.loads((await get_marketplace(request=MagicMock())).body)

    assert "installationPreference" not in marketplace["plugins"][0]


@pytest.mark.asyncio
async def test_update_plugin_propagates_installation_preference_to_get_and_list():
    name = "updated-install-plugin"
    await register_plugin(
        request=RegisterPluginRequest(name=name, source=_ARCHIVE_SOURCE),
        user_api_key_dict=_USER,
    )

    response = await update_plugin(
        plugin_name=name,
        request=UpdatePluginRequest(
            source=_ARCHIVE_SOURCE,
            installation_preference="auto_install",
        ),
        user_api_key_dict=_USER,
    )

    plugin = await get_plugin(plugin_name=name, user_api_key_dict=_USER)
    listed_plugin = (await list_plugins(user_api_key_dict=_USER)).plugins[0]

    assert response.plugin.installation_preference == "auto_install"
    assert plugin["installation_preference"] == "auto_install"
    assert listed_plugin.installation_preference == "auto_install"


@pytest.mark.asyncio
async def test_update_plugin_without_installation_preference_clears_it():
    name = "cleared-install-plugin"
    await register_plugin(
        request=RegisterPluginRequest(name=name, source=_ARCHIVE_SOURCE, installation_preference="auto_install"),
        user_api_key_dict=_USER,
    )

    await update_plugin(
        plugin_name=name,
        request=UpdatePluginRequest(source=_ARCHIVE_SOURCE),
        user_api_key_dict=_USER,
    )

    marketplace = json.loads((await get_marketplace(request=MagicMock())).body)
    assert "installationPreference" not in marketplace["plugins"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_value", ["auto-install", 1, True, None])
async def test_stored_unknown_installation_preference_is_not_served(stored_value, caplog):
    name = "hand-edited-plugin"
    table = litellm.proxy.proxy_server.prisma_client.db.litellm_claudecodeplugintable
    await table.create(
        data={
            "name": name,
            "manifest_json": json.dumps(
                {"name": name, "source": _ARCHIVE_SOURCE, "installation_preference": stored_value}
            ),
        }
    )

    marketplace = json.loads((await get_marketplace(request=MagicMock())).body)
    listed_plugin = (await list_plugins(user_api_key_dict=_USER)).plugins[0]
    plugin = await get_plugin(plugin_name=name, user_api_key_dict=_USER)

    assert marketplace["plugins"] == [{"name": name, "source": _ARCHIVE_SOURCE}]
    assert listed_plugin.installation_preference is None
    assert plugin["installation_preference"] is None
    unknown_value_warnings = [r for r in caplog.records if "unknown installation_preference" in r.getMessage()]
    assert len(unknown_value_warnings) == (0 if stored_value is None else 3)


@pytest.mark.asyncio
async def test_register_accepts_the_camel_case_installation_preference_key():
    request = RegisterPluginRequest.model_validate(
        {"name": "s3-skill", "source": _ARCHIVE_SOURCE, "installationPreference": "auto_install"}
    )

    response = await register_plugin(request=request, user_api_key_dict=_USER)
    marketplace = json.loads((await get_marketplace(request=MagicMock())).body)

    assert response.plugin.installation_preference == "auto_install"
    assert (await _read_stored_manifest("s3-skill"))["installation_preference"] == "auto_install"
    assert marketplace["plugins"][0]["installationPreference"] == "auto_install"


_NON_ARCHIVE_SOURCES = [
    {"source": "github", "repo": "org/repo"},
    {"source": "url", "url": "https://github.com/org/repo.git"},
    _GIT_SUBDIR_SOURCE,
]
_UNPINNED_ARCHIVE_SOURCE = {"source": "archive", "url": "https://artifacts.example.com/plugin.zip"}


async def _assert_preference_refused(source, preference):
    with pytest.raises(HTTPException) as register_error:
        await register_plugin(
            request=RegisterPluginRequest(name="no-op-plugin", source=source, installation_preference=preference),
            user_api_key_dict=_USER,
        )
    await register_plugin(request=RegisterPluginRequest(name="no-op-plugin", source=source), user_api_key_dict=_USER)
    with pytest.raises(HTTPException) as update_error:
        await update_plugin(
            plugin_name="no-op-plugin",
            request=UpdatePluginRequest(source=source, installation_preference=preference),
            user_api_key_dict=_USER,
        )

    assert register_error.value.status_code == 400
    assert update_error.value.status_code == 400
    assert "installationPreference" not in json.loads((await get_marketplace(request=MagicMock())).body)["plugins"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("preference", ["available", "auto_install", "required"])
@pytest.mark.parametrize("source", _NON_ARCHIVE_SOURCES)
async def test_every_preference_needs_an_archive_source(preference, source):
    await _assert_preference_refused(source, preference)


@pytest.mark.asyncio
@pytest.mark.parametrize("preference", ["auto_install", "required"])
async def test_self_installing_preference_needs_the_archive_sha256(preference):
    await _assert_preference_refused(_UNPINNED_ARCHIVE_SOURCE, preference)


@pytest.mark.asyncio
async def test_available_preference_is_accepted_on_an_unpinned_archive():
    response = await register_plugin(
        request=RegisterPluginRequest(
            name="listed-plugin", source=_UNPINNED_ARCHIVE_SOURCE, installation_preference="available"
        ),
        user_api_key_dict=_USER,
    )
    marketplace = json.loads((await get_marketplace(request=MagicMock())).body)

    assert response.plugin.installation_preference == "available"
    assert marketplace["plugins"][0]["installationPreference"] == "available"


@pytest.mark.parametrize(
    "build_request",
    [
        lambda body: RegisterPluginRequest.model_validate({"name": "s3-skill", **body}),
        UpdatePluginRequest.model_validate,
    ],
    ids=["register", "update"],
)
def test_plugin_request_rejects_both_preference_spellings(build_request):
    with pytest.raises(ValidationError, match="not both"):
        build_request(
            {
                "source": _ARCHIVE_SOURCE,
                "installation_preference": "auto_install",
                "installationPreference": "available",
            }
        )


@pytest.mark.parametrize(
    "build_request",
    [
        lambda body: RegisterPluginRequest.model_validate({"name": "s3-skill", **body}),
        UpdatePluginRequest.model_validate,
    ],
    ids=["register", "update"],
)
@pytest.mark.parametrize(
    "null_key, value_key",
    [("installation_preference", "installationPreference"), ("installationPreference", "installation_preference")],
)
def test_plugin_request_reads_the_spelling_that_carries_a_value(build_request, null_key, value_key):
    request = build_request({"source": _ARCHIVE_SOURCE, null_key: None, value_key: "auto_install"})

    assert request.installation_preference == "auto_install"


@pytest.mark.asyncio
async def test_register_plugin_git_subdir_missing_url():
    """git-subdir without url field raises HTTP 400."""
    request = RegisterPluginRequest(
        name="bad-plugin",
        source={"source": "git-subdir", "path": "plugins/my-plugin"},
    )

    with pytest.raises(HTTPException) as exc_info:
        await register_plugin(request=request, user_api_key_dict=_USER)

    assert exc_info.value.status_code == 400
    assert "url" in exc_info.value.detail["error"]


@pytest.mark.asyncio
async def test_register_plugin_git_subdir_empty_url():
    """git-subdir with empty url raises HTTP 400."""
    request = RegisterPluginRequest(
        name="bad-plugin",
        source={"source": "git-subdir", "url": "", "path": "plugins/my-plugin"},
    )

    with pytest.raises(HTTPException) as exc_info:
        await register_plugin(request=request, user_api_key_dict=_USER)

    assert exc_info.value.status_code == 400
    assert "url" in exc_info.value.detail["error"]


@pytest.mark.asyncio
async def test_register_plugin_git_subdir_missing_path():
    """git-subdir without path field raises HTTP 400."""
    request = RegisterPluginRequest(
        name="bad-plugin",
        source={"source": "git-subdir", "url": "https://github.com/org/monorepo.git"},
    )

    with pytest.raises(HTTPException) as exc_info:
        await register_plugin(request=request, user_api_key_dict=_USER)

    assert exc_info.value.status_code == 400
    assert "path" in exc_info.value.detail["error"]


@pytest.mark.asyncio
async def test_register_plugin_git_subdir_empty_path():
    """git-subdir with empty path raises HTTP 400."""
    request = RegisterPluginRequest(
        name="bad-plugin",
        source={
            "source": "git-subdir",
            "url": "https://github.com/org/monorepo.git",
            "path": "",
        },
    )

    with pytest.raises(HTTPException) as exc_info:
        await register_plugin(request=request, user_api_key_dict=_USER)

    assert exc_info.value.status_code == 400
    assert "path" in exc_info.value.detail["error"]


@pytest.mark.asyncio
async def test_register_plugin_git_subdir_path_traversal():
    """git-subdir with path traversal segments raises HTTP 400."""
    for bad_path in [
        "../../etc/passwd",
        "../secrets",
        "/absolute/path",
        "plugins\\..\\..\\secrets",  # backslash traversal
        "plugins/%2e%2e/secrets",  # percent-encoded traversal
        "plugins/%2E%2E/secrets",  # uppercase percent-encoded traversal
        "plugins/%252e%252e/secrets",  # double-encoded traversal
    ]:
        request = RegisterPluginRequest(
            name="bad-plugin",
            source={
                "source": "git-subdir",
                "url": "https://github.com/org/monorepo.git",
                "path": bad_path,
            },
        )

        with pytest.raises(HTTPException) as exc_info:
            await register_plugin(request=request, user_api_key_dict=_USER)

        assert exc_info.value.status_code == 400
        assert "relative" in exc_info.value.detail["error"]


@pytest.mark.asyncio
async def test_register_plugin_unknown_source_type():
    """Unknown source type raises HTTP 400 listing all valid types."""
    request = RegisterPluginRequest(
        name="bad-plugin",
        source={"source": "ftp", "url": "ftp://example.com/repo"},
    )

    with pytest.raises(HTTPException) as exc_info:
        await register_plugin(request=request, user_api_key_dict=_USER)

    assert exc_info.value.status_code == 400
    assert "git-subdir" in exc_info.value.detail["error"]
    assert "archive" in exc_info.value.detail["error"]


@pytest.mark.asyncio
async def test_archive_source_registers_and_is_served_verbatim_in_marketplace():
    response = await register_plugin(
        request=RegisterPluginRequest(name="s3-skill", source=_ARCHIVE_SOURCE),
        user_api_key_dict=_USER,
    )

    assert response.action == "created"
    assert response.plugin.source == _ARCHIVE_SOURCE

    marketplace = json.loads((await get_marketplace(request=MagicMock())).body)
    assert marketplace["plugins"] == [{"name": "s3-skill", "source": _ARCHIVE_SOURCE, "version": "1.0.0"}]


@pytest.mark.asyncio
async def test_archive_source_without_sha256_is_accepted():
    source = {"source": "archive", "url": "https://artifacts.example.com/plugin.zip"}

    response = await register_plugin(
        request=RegisterPluginRequest(name="unpinned-skill", source=source),
        user_api_key_dict=_USER,
    )

    assert response.plugin.source == source


@pytest.mark.asyncio
async def test_update_plugin_to_archive_source():
    name = "my-monorepo-plugin"
    await register_plugin(
        request=RegisterPluginRequest(name=name, source=_GIT_SUBDIR_SOURCE),
        user_api_key_dict=_USER,
    )

    response = await update_plugin(
        plugin_name=name,
        request=UpdatePluginRequest(source=_ARCHIVE_SOURCE),
        user_api_key_dict=_USER,
    )

    assert response.action == "updated"
    assert (await _read_stored_manifest(name))["source"] == _ARCHIVE_SOURCE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source, expected_fragment",
    [
        ({"source": "archive"}, "url"),
        ({"source": "archive", "url": ""}, "url"),
        ({"source": "archive", "url": "http://artifacts.example.com/plugin.zip"}, "https"),
        ({"source": "archive", "url": "s3://skills-bucket/plugin.zip"}, "https"),
        ({"source": "archive", "url": "https://"}, "https"),
        ({"source": "archive", "url": "https:///plugin.zip"}, "https"),
        ({"source": "archive", "url": "https://[::1/plugin.zip"}, "https"),
        ({"source": "archive", "url": "https://artifacts.example.com/plugin.zip", "sha256": "a" * 63}, "sha256"),
        ({"source": "archive", "url": "https://artifacts.example.com/plugin.zip", "sha256": "a" * 65}, "sha256"),
        ({"source": "archive", "url": "https://artifacts.example.com/plugin.zip", "sha256": "g" * 64}, "sha256"),
    ],
)
async def test_register_plugin_archive_rejects_malformed_source(source, expected_fragment):
    with pytest.raises(HTTPException) as exc_info:
        await register_plugin(request=RegisterPluginRequest(name="bad-plugin", source=source), user_api_key_dict=_USER)

    assert exc_info.value.status_code == 400
    assert expected_fragment in exc_info.value.detail["error"]


@pytest.mark.asyncio
async def test_member_create_is_staged_outside_the_catalog():
    request: Final = RegisterPluginRequest(name="proposed-skill", source=_GIT_SUBDIR_SOURCE)
    response: Final = await register_plugin(request=request, user_api_key_dict=_NON_ADMIN_USER)
    assert (response.status, response.action, response.plugin.enabled, response.submission_id) == (
        "pending_review",
        "submitted",
        False,
        "submission-id",
    )
    table: Final = litellm.proxy.proxy_server.prisma_client.db.litellm_claudecodeplugintable
    assert await table.find_unique(where={"name": request.name}) is None
    assert (await list_plugins(user_api_key_dict=_USER)).plugins == []


@pytest.mark.asyncio
async def test_member_update_keeps_the_published_source():
    name: Final = "trusted-plugin"
    await register_plugin(
        request=RegisterPluginRequest(name=name, source=_GIT_SUBDIR_SOURCE, version="1.0.0"),
        user_api_key_dict=_USER,
    )
    proposed_source: Final = {"source": "github", "repo": "org/proposal"}
    response: Final = await update_plugin(
        plugin_name=name,
        request=UpdatePluginRequest(source=proposed_source),
        user_api_key_dict=_NON_ADMIN_USER,
    )
    assert (response.status, response.plugin.source, response.plugin.enabled) == (
        "pending_review",
        proposed_source,
        False,
    )
    assert (await _read_stored_manifest(name))["source"] == _GIT_SUBDIR_SOURCE


@pytest.mark.asyncio
async def test_enable_disable_delete_plugin_reject_non_admin():
    """Non-admin keys cannot enable, disable, or delete catalog entries."""
    name = "trusted-plugin-2"
    await register_plugin(
        request=RegisterPluginRequest(name=name, source=_GIT_SUBDIR_SOURCE, version="1.0.0"),
        user_api_key_dict=_USER,
    )

    for coro in (
        enable_plugin(plugin_name=name, user_api_key_dict=_NON_ADMIN_USER),
        disable_plugin(plugin_name=name, user_api_key_dict=_NON_ADMIN_USER),
        delete_plugin(plugin_name=name, user_api_key_dict=_NON_ADMIN_USER),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await coro
        assert exc_info.value.status_code == 403

    table = litellm.proxy.proxy_server.prisma_client.db.litellm_claudecodeplugintable
    assert (await table.find_unique(where={"name": name})).enabled is True


@pytest.mark.asyncio
async def test_register_plugin():
    plugin_name: Final = "test-plugin"

    request: Final = RegisterPluginRequest(
        name=plugin_name,
        source={"source": "github", "repo": "test-org/test-repo"},
        version="1.0.0",
        description="Test plugin for unit tests",
    )

    user_api_key_dict: Final = UserAPIKeyAuth(
        user_role=LitellmUserRoles.PROXY_ADMIN,
        api_key=MASTER_KEY,
        user_id="test-user",
    )

    response: Final = await register_plugin(
        request=request,
        user_api_key_dict=user_api_key_dict,
    )

    assert response.status == "success"
    assert response.action == "created"
    assert response.plugin.name == plugin_name
    assert response.plugin.version == "1.0.0"
    assert response.plugin.enabled is True

    stored_plugin: Final = await litellm.proxy.proxy_server.prisma_client.db.litellm_claudecodeplugintable.find_unique(
        where={"name": plugin_name}
    )
    assert stored_plugin is not None
    assert stored_plugin.name == plugin_name


@pytest.mark.asyncio
async def test_get_marketplace():
    plugin_name: Final = "test-marketplace-plugin"

    request: Final = RegisterPluginRequest(
        name=plugin_name,
        source={"source": "github", "repo": "test-org/marketplace-test"},
        version="2.0.0",
        description="Test plugin for marketplace test",
    )

    user_api_key_dict: Final = UserAPIKeyAuth(
        user_role=LitellmUserRoles.PROXY_ADMIN,
        api_key=MASTER_KEY,
        user_id="test-user",
    )

    await register_plugin(
        request=request,
        user_api_key_dict=user_api_key_dict,
    )

    response: Final = await get_marketplace(request=MagicMock())

    body: Final = json.loads(response.body.decode())

    assert body["name"] == "litellm"
    assert "plugins" in body

    our_plugin: Final = next((p for p in body["plugins"] if p["name"] == plugin_name), None)
    assert our_plugin is not None
    assert our_plugin["source"] == {
        "source": "github",
        "repo": "test-org/marketplace-test",
    }
    assert our_plugin["version"] == "2.0.0"


@pytest.mark.asyncio
async def test_register_plugin_git_subdir():
    plugin_name: Final = "test-subdir-plugin"

    request: Final = RegisterPluginRequest(
        name=plugin_name,
        source={
            "source": "git-subdir",
            "url": "https://github.com/test-org/monorepo.git",
            "path": "plugins/my-plugin",
        },
        version="1.0.0",
        description="Test git-subdir plugin",
    )

    user_api_key_dict: Final = UserAPIKeyAuth(
        user_role=LitellmUserRoles.PROXY_ADMIN,
        api_key=MASTER_KEY,
        user_id="test-user",
    )

    response: Final = await register_plugin(
        request=request,
        user_api_key_dict=user_api_key_dict,
    )

    assert response.status == "success"
    assert response.action == "created"
    assert response.plugin.name == plugin_name
    assert response.plugin.source["source"] == "git-subdir"
    assert response.plugin.source["url"] == "https://github.com/test-org/monorepo.git"
    assert response.plugin.source["path"] == "plugins/my-plugin"
    assert response.plugin.enabled is True


def test_marketplace_response_round_trips_installation_preference():
    catalog: Final = {
        "name": "litellm",
        "owner": {"name": "LiteLLM", "email": "support@litellm.ai"},
        "plugins": [
            {
                "name": "auto",
                "source": {"source": "url", "url": "https://example.com/auto.tgz"},
                "installationPreference": "auto_install",
            },
            {"name": "manual", "source": {"source": "url", "url": "https://example.com/manual.tgz"}},
        ],
    }

    round_tripped: Final = MarketplaceResponse.model_validate(catalog).model_dump(exclude_none=True)

    assert round_tripped["plugins"][0]["installationPreference"] == "auto_install"
    assert "installationPreference" not in round_tripped["plugins"][1]


def test_marketplace_response_rejects_unknown_installation_preference():
    catalog: Final = {
        "name": "litellm",
        "owner": {"name": "LiteLLM", "email": "support@litellm.ai"},
        "plugins": [
            {
                "name": "auto",
                "source": {"source": "url", "url": "https://example.com/auto.tgz"},
                "installationPreference": "sometimes",
            }
        ],
    }

    with pytest.raises(ValidationError):
        MarketplaceResponse.model_validate(catalog)


def _require_marketplace_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        litellm.proxy.proxy_server, "general_settings", {"claude_code_marketplace_auth_required": True}
    )
    monkeypatch.setattr(litellm.proxy.proxy_server, "user_api_key_cache", DualCache())


async def _get_marketplace_over_http(
    headers: Mapping[str, str] = MappingProxyType({}), params: Mapping[str, str] = MappingProxyType({})
) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=litellm.proxy.proxy_server.app), base_url="http://test"
    ) as client:
        return await client.get("/claude-code/marketplace.json", headers=dict(headers), params=dict(params))


@pytest.mark.asyncio
async def test_marketplace_auth_required_refuses_an_anonymous_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    _require_marketplace_auth(monkeypatch)
    await _register_public_and_private_plugins()

    response: Final = await _get_marketplace_over_http()

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert "public-skill" not in response.text


@pytest.mark.asyncio
async def test_marketplace_auth_required_refuses_a_key_in_the_query_string(monkeypatch: pytest.MonkeyPatch) -> None:
    _require_marketplace_auth(monkeypatch)
    await _register_public_and_private_plugins()

    response: Final = await _get_marketplace_over_http(params={"key": MASTER_KEY})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert "public-skill" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": f"Bearer {MASTER_KEY}"},
        {"x-litellm-api-key": f"Bearer {MASTER_KEY}"},
    ],
)
async def test_marketplace_auth_required_serves_a_caller_with_a_valid_key(
    monkeypatch: pytest.MonkeyPatch, headers: Mapping[str, str]
) -> None:
    _require_marketplace_auth(monkeypatch)
    await _register_public_and_private_plugins()

    response: Final = await _get_marketplace_over_http(headers)

    assert response.status_code == 200
    assert sorted(plugin["name"] for plugin in response.json()["plugins"]) == ["private-skill", "public-skill"]


@pytest.mark.asyncio
async def test_marketplace_auth_required_refuses_an_unknown_bearer_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _require_marketplace_auth(monkeypatch)
    litellm.proxy.proxy_server.prisma_client.get_data = AsyncMock(return_value=None)
    await _register_public_and_private_plugins()

    response: Final = await _get_marketplace_over_http({"Authorization": "Bearer sk-not-a-litellm-key"})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert "public-skill" not in response.text


_LIMITED_KEY: Final = "sk-limited"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limited_key_auth, status_code",
    [
        (
            UserAPIKeyAuth(
                token=hash_token(_LIMITED_KEY),
                user_role=LitellmUserRoles.INTERNAL_USER,
                allowed_routes=["/chat/completions"],
            ),
            403,
        ),
        (
            UserAPIKeyAuth(
                token=hash_token(_LIMITED_KEY), user_role=LitellmUserRoles.INTERNAL_USER, spend=2.0, max_budget=1.0
            ),
            422,
        ),
    ],
    ids=["route-not-allowed", "over-budget"],
)
async def test_marketplace_auth_required_keeps_a_valid_key_refusal_status(
    monkeypatch: pytest.MonkeyPatch, limited_key_auth: UserAPIKeyAuth, status_code: int
) -> None:
    _require_marketplace_auth(monkeypatch)
    litellm.proxy.proxy_server.user_api_key_cache.set_cache(key=hash_token(_LIMITED_KEY), value=limited_key_auth)
    await _register_public_and_private_plugins()

    response: Final = await _get_marketplace_over_http({"Authorization": f"Bearer {_LIMITED_KEY}"})

    assert response.status_code == status_code
    assert "www-authenticate" not in response.headers
    assert "public-skill" not in response.text


@pytest.mark.asyncio
async def test_marketplace_without_auth_required_ignores_an_unrelated_authorization_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(litellm.proxy.proxy_server, "general_settings", {})
    await _register_public_and_private_plugins()

    response: Final = await _get_marketplace_over_http({"Authorization": "Bearer an-identity-aware-proxy-token"})

    assert response.status_code == 200
    assert [plugin["name"] for plugin in response.json()["plugins"]] == ["public-skill"]


@pytest.mark.asyncio
async def test_marketplace_500_body_carries_no_exception_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(litellm.proxy.proxy_server, "general_settings", {})
    table: Final = litellm.proxy.proxy_server.prisma_client.db.litellm_claudecodeplugintable
    table.find_many = AsyncMock(side_effect=RuntimeError("could not reach postgres://admin:hunter2@db.internal"))

    response: Final = await _get_marketplace_over_http()

    assert response.status_code == 500
    assert "hunter2" not in response.text
    assert "db.internal" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", ["true", "True", 1, "a typo"])
async def test_marketplace_auth_required_treats_a_quoted_or_unreadable_setting_as_on(
    monkeypatch: pytest.MonkeyPatch, setting: object
) -> None:
    monkeypatch.setattr(
        litellm.proxy.proxy_server, "general_settings", {"claude_code_marketplace_auth_required": setting}
    )
    monkeypatch.setattr(litellm.proxy.proxy_server, "user_api_key_cache", DualCache())
    await _register_public_and_private_plugins()

    response: Final = await _get_marketplace_over_http()

    assert response.status_code == 401
    assert "public-skill" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", ["false", False, 0, None])
async def test_marketplace_auth_required_off_values_keep_the_public_catalog(
    monkeypatch: pytest.MonkeyPatch, setting: object
) -> None:
    monkeypatch.setattr(
        litellm.proxy.proxy_server, "general_settings", {"claude_code_marketplace_auth_required": setting}
    )
    await _register_public_and_private_plugins()

    response: Final = await _get_marketplace_over_http()

    assert response.status_code == 200
    assert [plugin["name"] for plugin in response.json()["plugins"]] == ["public-skill"]


@pytest.mark.asyncio
async def test_marketplace_auth_required_refuses_the_database_outage_fallback_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        litellm.proxy.proxy_server,
        "general_settings",
        {"claude_code_marketplace_auth_required": True, "allow_requests_on_db_unavailable": True},
    )
    monkeypatch.setattr(litellm.proxy.proxy_server, "user_api_key_cache", DualCache())
    await _register_public_and_private_plugins()
    litellm.proxy.proxy_server.prisma_client.get_data = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    litellm.proxy.proxy_server.prisma_client.attempt_db_reconnect = AsyncMock(return_value=False)

    response: Final = await _get_marketplace_over_http({"Authorization": "Bearer sk-not-a-litellm-key"})

    assert response.status_code == 503
    assert "www-authenticate" not in response.headers
    assert "public-skill" not in response.text


@pytest.mark.asyncio
async def test_marketplace_without_a_database_answers_db_not_connected_for_a_query_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(litellm.proxy.proxy_server, "general_settings", {})
    monkeypatch.setattr(litellm.proxy.proxy_server, "prisma_client", None)
    monkeypatch.setattr(litellm.proxy.proxy_server, "user_api_key_cache", DualCache())

    response: Final = await _get_marketplace_over_http(params={"key": "sk-not-the-master-key"})

    assert response.status_code == 500
    assert CommonProxyErrors.db_not_connected_error.value in response.text


_UploadClient: TypeAlias = tuple[httpx.AsyncClient, MagicMock, MagicMock]
_UPLOAD_NAME: Final = "local-skill"
_UPLOAD_DIGEST: Final = hashlib.sha256(b"stored archive").hexdigest()
_UPLOAD_PATH: Final = f"/claude-code/plugins/{_UPLOAD_NAME}/archive/{_UPLOAD_DIGEST}.zip"
_UPLOAD_SOURCE: Final = {"source": "archive", "url": f"http://test{_UPLOAD_PATH}", "sha256": _UPLOAD_DIGEST}


@pytest.fixture
async def upload_storage(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_UploadClient]:
    prisma: Final = litellm.proxy.proxy_server.prisma_client
    prisma.writer_db = MagicMock()
    prisma.writer_db.tx.return_value.__aenter__.return_value = prisma.writer_db
    plugins: Final = prisma.writer_db.litellm_claudecodeplugintable
    archives: Final = prisma.writer_db.litellm_claudecodepluginupload
    prisma.writer_db.query_raw = AsyncMock(return_value=[])
    prisma.writer_db.litellm_claudecodepluginversion.create = AsyncMock()
    archives.create = AsyncMock()
    plugins.create = AsyncMock(
        return_value=prisma_models.LiteLLM_ClaudeCodePluginTable(
            id="uploaded-id",
            name=_UPLOAD_NAME,
            enabled=True,
            version="2.0.0",
        )
    )
    plugins.find_unique = AsyncMock(
        return_value=prisma_models.LiteLLM_ClaudeCodePluginTable(
            id="uploaded-id",
            name=_UPLOAD_NAME,
            enabled=True,
            manifest_json=json.dumps({"source": _UPLOAD_SOURCE}),
        )
    )
    archives.find_first = AsyncMock(
        return_value=prisma_models.LiteLLM_ClaudeCodePluginUpload(
            plugin_name=_UPLOAD_NAME,
            content=Base64.encode(b"stored archive"),
        )
    )
    monkeypatch.setattr(
        litellm.proxy.proxy_server.app,
        "dependency_overrides",
        {
            get_plugin_upload_repository: lambda: PluginUploadRepository(prisma),
            get_skill_submission_repository: lambda: SkillSubmissionRepository(prisma),
        },
    )
    monkeypatch.setattr(litellm.proxy.proxy_server, "general_settings", {})
    monkeypatch.setattr(litellm.proxy.proxy_server, "user_api_key_cache", DualCache())
    monkeypatch.setenv("PROXY_BASE_URL", "http://test")
    monkeypatch.delenv("SERVER_ROOT_PATH", raising=False)
    for key, skills in ((_LIMITED_KEY, [_UPLOAD_NAME]), ("sk-plain", [])):
        litellm.proxy.proxy_server.user_api_key_cache.set_cache(
            key=hash_token(key),
            value=_granted_user(skills).model_copy(update={"token": hash_token(key)}),
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=litellm.proxy.proxy_server.app),
        base_url="http://test",
    ) as client:
        yield client, plugins, archives


async def _post_skill_upload(
    client: httpx.AsyncClient, content: bytes, metadata: str, key: str = MASTER_KEY,
) -> httpx.Response:
    return await client.post(
        "/claude-code/plugins/upload", headers={"Authorization": f"Bearer {key}"},
        data={"metadata": metadata}, files={"file": ("local.zip", content, "application/zip")},
    )


@pytest.mark.asyncio
async def test_skill_upload_creates_metadata_and_bytes_atomically_and_rejects_duplicates(
    upload_storage: _UploadClient,
) -> None:
    client, plugins, archives = upload_storage
    plugins.find_unique = AsyncMock(return_value=None)
    buffer: Final = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("SKILL.md", "Read the personal instructions")
    metadata: Final = {"name": _UPLOAD_NAME, "description": "My notes", "namespace": "personal", "version": "2.0.0"}
    response: Final = await _post_skill_upload(client, buffer.getvalue(), json.dumps(metadata))
    assert response.status_code == 200, response.text
    plugins.create.assert_awaited_once()
    stored: Final = plugins.create.await_args.kwargs["data"]
    content: Final = archives.create.await_args.kwargs["data"]["content"].decode()
    digest: Final = hashlib.sha256(content).hexdigest()
    source: Final = {
        "source": "archive",
        "url": f"http://test/claude-code/plugins/{_UPLOAD_NAME}/archive/{digest}.zip",
        "sha256": digest,
    }
    assert json.loads(stored["manifest_json"]) == {**metadata, "source": source}
    assert stored == {
        "name": _UPLOAD_NAME, "description": "My notes", "version": "2.0.0",
        "manifest_json": stored["manifest_json"], "created_by": litellm.proxy.proxy_server.litellm_proxy_admin_name,
        "files_json": "{}", "updated_at": stored["updated_at"],
    }
    assert response.json() == {"status": "success", "action": "created", "submission_id": None, "plugin": {
        "id": "uploaded-id", "name": _UPLOAD_NAME, "version": "2.0.0", "description": "My notes",
        "source": source, "installation_preference": None, "enabled": True,
    }}
    plugins.create.side_effect = UniqueViolationError({"message": "duplicate", "error_code": "P2002"})
    duplicate: Final = await _post_skill_upload(client, buffer.getvalue(), json.dumps(metadata))
    assert duplicate.status_code == 409, duplicate.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content, metadata, key, status",
    [
        (b"invalid", '{"name":"local-skill"}', MASTER_KEY, 400),
        (b"x" * (10 * 1024 * 1024 + 1), '{"name":"local-skill"}', MASTER_KEY, 413),
        (b"ignored", "not-json", MASTER_KEY, 400),
        (b"ignored", '{"name":"../invalid"}', MASTER_KEY, 422),
        (b"ignored", '{"name":"local-skill"}', _LIMITED_KEY, 400),
    ],
    ids=["invalid-zip", "file-over-cap", "invalid-metadata", "invalid-name", "member-invalid-zip"],
)
async def test_skill_upload_rejects_invalid_or_unauthorized_input_without_writing(
    upload_storage: _UploadClient,
    content: bytes,
    metadata: str,
    key: str,
    status: int,
) -> None:
    client, plugins, _ = upload_storage
    response: Final = await _post_skill_upload(client, content, metadata, key)
    assert response.status_code == status, response.text
    plugins.create.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled, key, source, blob, status, reads", [
    (True, None, _UPLOAD_SOURCE, True, 200, 1),
    (False, None, _UPLOAD_SOURCE, True, 404, 0),
    (False, "sk-plain", _UPLOAD_SOURCE, True, 404, 0),
    (False, _LIMITED_KEY, _UPLOAD_SOURCE, True, 200, 1),
    (True, None, None, True, 404, 0),
    (True, None, {**_UPLOAD_SOURCE, "sha256": "0" * 64}, True, 404, 0),
    (True, None, {**_UPLOAD_SOURCE, "url": "https://external.example/skill.zip"}, True, 404, 0),
    (True, None, _UPLOAD_SOURCE, False, 404, 1),
])
async def test_archive_reads_only_authorized_current_bytes(
    upload_storage: _UploadClient, enabled: bool, key: str | None,
    source: Mapping[str, str] | None, blob: bool, status: int, reads: int,
) -> None:
    client, plugins, archives = upload_storage
    manifest: Final = json.dumps({"source": source})
    plugins.find_unique.return_value = None if source is None else prisma_models.LiteLLM_ClaudeCodePluginTable(
        id="uploaded-id", name=_UPLOAD_NAME, enabled=enabled, manifest_json=manifest,
    )
    if not blob:
        archives.find_first.return_value = None
    response: Final = await client.get(_UPLOAD_PATH, headers={"Authorization": f"Bearer {key}"} if key else {})
    assert response.status_code == status, response.text
    assert (response.content == b"stored archive") is (status == 200)
    plugins.find_unique.assert_awaited_once_with(where={"name": _UPLOAD_NAME})
    assert archives.find_first.await_args_list == ([call(where={
        "plugin_name": _UPLOAD_NAME, "plugin": {"is": {"manifest_json": manifest, "enabled": enabled}},
    })] if reads else [])
    litellm.proxy.proxy_server.prisma_client.db.litellm_claudecodeplugintable.find_unique.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("required, header, credential, query, status", [
    (True, "Authorization", _LIMITED_KEY, None, 200),
    (False, "x-skill-key", _LIMITED_KEY, MASTER_KEY, 200),
    (True, "x-skill-key", _LIMITED_KEY, MASTER_KEY, 200),
    (False, "x-skill-key", "", MASTER_KEY, 401),
    (False, None, None, MASTER_KEY, 404),
    (False, None, None, "invalid-query-key", 404),
    (True, None, None, MASTER_KEY, 401),
    (True, None, None, None, 401),
])
async def test_archive_requires_header_credentials_for_private_content(
    upload_storage: _UploadClient, monkeypatch: pytest.MonkeyPatch, required: bool,
    header: str | None, credential: str | None, query: str | None, status: int,
) -> None:
    client, plugins, archives = upload_storage
    plugins.find_unique.return_value.enabled = False
    monkeypatch.setattr(litellm.proxy.proxy_server, "general_settings", {
        "litellm_key_header_name": "X-Skill-Key", "claude_code_marketplace_auth_required": required,
    })
    response: Final = await client.get(
        _UPLOAD_PATH, headers={header: f"Bearer {credential}" if credential else ""} if header else {},
        params={"key": query} if query else {},
    )
    assert response.status_code == status, response.text
    assert (response.content == b"stored archive") is (status == 200)
    assert archives.find_first.await_count == int(status == 200)


class _SubmissionRows:
    def __init__(self) -> None:
        self.rows: tuple[prisma_models.LiteLLM_SkillSubmission, ...] = ()

    async def create(self, data: Mapping[str, object]) -> prisma_models.LiteLLM_SkillSubmission:
        row: Final = prisma_models.LiteLLM_SkillSubmission.model_validate(
            {
                "id": f"submission-{len(self.rows)}",
                "status": "pending_review",
                "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
                **data,
            }
        )
        self.rows = (*self.rows, row)
        return row

    async def find_unique(self, where: Mapping[str, object]) -> prisma_models.LiteLLM_SkillSubmission | None:
        return next((row for row in self.rows if row.id == where["id"]), None)

    async def find_first(self, where: Mapping[str, object]) -> prisma_models.LiteLLM_SkillSubmission | None:
        row: Final = await self.find_unique(where)
        return None if row is None or where.get("submitted_by", row.submitted_by) != row.submitted_by else row

    async def update(
        self,
        where: Mapping[str, object],
        data: Mapping[str, object],
    ) -> prisma_models.LiteLLM_SkillSubmission | None:
        current: Final = await self.find_unique(where)
        if current is None:
            return None
        updated: Final = prisma_models.LiteLLM_SkillSubmission.model_validate({**current.model_dump(), **data})
        self.rows = tuple(updated if row.id == current.id else row for row in self.rows)
        return updated

    async def query_raw(self, query: str, *args: object) -> tuple[Mapping[str, object], ...]:
        if 'FROM "LiteLLM_SkillSubmission"' not in query or "FOR UPDATE" in query:
            return ()
        records: Final = (
            tuple(row for row in self.rows if row.id == args[0] and (args[1] is None or row.submitted_by == args[1]))
            if "WHERE id = $1" in query
            else tuple(row for row in self.rows if args[0] is None or row.submitted_by == args[0])
        )
        return tuple({**row.model_dump(), "has_archive": row.archive_content is not None} for row in records)


@pytest.fixture
async def submission_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    from fastapi import FastAPI, Header

    database: Final = litellm.proxy.proxy_server.prisma_client
    rows: Final = _SubmissionRows()
    database.writer_db.litellm_skillsubmission = rows
    database.writer_db.query_raw = rows.query_raw
    callers: Final = {
        "member": _NON_ADMIN_USER,
        "admin": _USER,
        "other": _NON_ADMIN_USER.model_copy(update={"user_id": "other"}),
        "viewer": _USER.model_copy(update={"user_role": LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY}),
        "readonly": _NON_ADMIN_USER.model_copy(update={"user_role": LitellmUserRoles.INTERNAL_USER_VIEW_ONLY}),
        "no-owner": _NON_ADMIN_USER.model_copy(update={"user_id": None}),
    }

    async def caller(x_test_user: str = Header(default="member")) -> UserAPIKeyAuth:
        return callers[x_test_user]

    app: Final = FastAPI()
    app.include_router(claude_code_marketplace.router)
    app.dependency_overrides[claude_code_marketplace.user_api_key_auth] = caller
    app.dependency_overrides[get_plugin_upload_repository] = lambda: PluginUploadRepository(database)
    app.dependency_overrides[get_skill_submission_repository] = lambda: SkillSubmissionRepository(database)
    monkeypatch.setattr(litellm.proxy.proxy_server, "general_settings", {})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_submission_owner_admin_and_viewer_access_with_atomic_review(
    submission_client: httpx.AsyncClient,
) -> None:
    client: Final = submission_client
    payload: Final = {"name": "reviewable-skill", "source": _GIT_SUBDIR_SOURCE, "domain": "Tools", "namespace": "work"}
    created: Final = await client.post("/claude-code/plugins", json=payload)
    assert created.status_code == 200, created.text
    path: Final = f"/claude-code/skill-submissions/{created.json()['submission_id']}"
    assert (await list_plugins(user_api_key_dict=_USER)).plugins == []
    assert (await client.get(path, headers={"x-test-user": "other"})).status_code == 404
    assert (await client.get("/claude-code/skill-submissions", headers={"x-test-user": "other"})).json() == {
        "submissions": [],
        "count": 0,
    }
    for identity in ("member", "viewer", "readonly"):
        denied: Final = await client.put(f"{path}/approve", headers={"x-test-user": identity})
        assert denied.status_code == 403, denied.text
    detail: Final = await client.get(path, headers={"x-test-user": "viewer"})
    assert detail.status_code == 200, detail.text
    assert detail.json()["submission"]["proposal"]["domain"] == payload["domain"]
    approved: Final = await client.put(f"{path}/approve", headers={"x-test-user": "admin"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["submission"]["status"] == "approved"
    assert (await _read_stored_manifest(payload["name"])) == {**payload, "version": "1.0.0"}
    again: Final = await client.put(f"{path}/reject", json={"reason": "too late"}, headers={"x-test-user": "admin"})
    assert again.status_code == 409, again.text
    assert (await client.get(path)).json()["submission"]["status"] == "approved"


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["viewer", "readonly", "no-owner"])
async def test_readonly_or_unowned_callers_cannot_submit(submission_client: httpx.AsyncClient, identity: str) -> None:
    result: Final = await submission_client.post(
        "/claude-code/plugins",
        json={"name": "forbidden-skill", "source": _GIT_SUBDIR_SOURCE},
        headers={"x-test-user": identity},
    )
    assert result.status_code == 403, result.text
    assert (await submission_client.get("/claude-code/skill-submissions")).json() == {"submissions": [], "count": 0}


@pytest.mark.asyncio
async def test_private_update_and_stale_editor_do_not_create_proposals(submission_client: httpx.AsyncClient) -> None:
    await _register_public_and_private_plugins()
    hidden: Final = await submission_client.put(
        "/claude-code/plugins/private-skill",
        json={"source": _GIT_SUBDIR_SOURCE},
    )
    missing: Final = await submission_client.put(
        "/claude-code/plugins/missing-skill",
        json={"source": _GIT_SUBDIR_SOURCE},
    )
    assert (
        (hidden.status_code, hidden.json())
        == (missing.status_code, missing.json())
        == (
            404,
            {"detail": {"error": "Skill not found"}},
        )
    )
    stale: Final = await submission_client.put(
        "/claude-code/plugins/public-skill",
        json={"source": _GIT_SUBDIR_SOURCE, "expected_updated_at": "2025-01-01T00:00:00Z"},
    )
    assert stale.status_code == 409, stale.text
    assert (await submission_client.get("/claude-code/skill-submissions")).json() == {"submissions": [], "count": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revision", "identity"])
async def test_stale_proposal_cannot_overwrite_a_changed_or_recreated_skill(
    submission_client: httpx.AsyncClient,
    change: str,
) -> None:
    name: Final = "stale-skill"
    await register_plugin(request=RegisterPluginRequest(name=name, source=_GIT_SUBDIR_SOURCE), user_api_key_dict=_USER)
    proposed: Final = await submission_client.put(
        f"/claude-code/plugins/{name}",
        json={"source": {"source": "github", "repo": "org/proposal"}},
    )
    table: Final = litellm.proxy.proxy_server.prisma_client.writer_db.litellm_claudecodeplugintable
    await table.update(
        where={"name": name},
        data=(
            {"updated_at": datetime(2026, 2, 1, tzinfo=timezone.utc)} if change == "revision" else {"id": "recreated"}
        ),
    )
    path: Final = f"/claude-code/skill-submissions/{proposed.json()['submission_id']}"
    result: Final = await submission_client.put(f"{path}/approve", headers={"x-test-user": "admin"})
    assert result.status_code == 409, result.text
    assert (await _read_stored_manifest(name))["source"] == _GIT_SUBDIR_SOURCE
    assert (await submission_client.get(path)).json()["submission"]["status"] == "pending_review"


@pytest.mark.asyncio
async def test_member_proposal_preserves_uploaded_http_source_and_optional_version(
    submission_client: httpx.AsyncClient,
) -> None:
    table: Final = litellm.proxy.proxy_server.prisma_client.writer_db.litellm_claudecodeplugintable
    await table.create(
        data={"name": _UPLOAD_NAME, "version": "1.0.0", "manifest_json": json.dumps({"source": _UPLOAD_SOURCE})}
    )
    proposal: Final = await submission_client.put(
        f"/claude-code/plugins/{_UPLOAD_NAME}",
        json={"source": _UPLOAD_SOURCE, "description": "new metadata"},
    )
    assert proposal.status_code == 200, proposal.text
    path: Final = f"/claude-code/skill-submissions/{proposal.json()['submission_id']}"
    detail: Final = (await submission_client.get(path)).json()["submission"]
    assert detail["proposal"]["source"] == _UPLOAD_SOURCE
    assert detail["proposal"]["version"] is None
    result: Final = await submission_client.put(
        f"{path}/reject",
        json={"reason": "Needs supporting files"},
        headers={"x-test-user": "admin"},
    )
    assert result.status_code == 200, result.text
    assert result.json()["submission"]["rejection_reason"] == "Needs supporting files"
    assert (await _read_stored_manifest(_UPLOAD_NAME)) == {"source": _UPLOAD_SOURCE}


@pytest.mark.asyncio
@pytest.mark.parametrize("updating", [False, True])
async def test_member_zip_is_only_readable_through_owned_submission(
    submission_client: httpx.AsyncClient,
    updating: bool,
) -> None:
    name: Final = "proposed-archive"
    if updating:
        await register_plugin(
            request=RegisterPluginRequest(name=name, source=_GIT_SUBDIR_SOURCE), user_api_key_dict=_USER
        )
    buffer: Final = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("SKILL.md", "Private proposal instructions")
    response: Final = await submission_client.request(
        "PUT" if updating else "POST",
        f"/claude-code/plugins/{name}/upload" if updating else "/claude-code/plugins/upload",
        data={"metadata": json.dumps({"description": "Review me"} if updating else {"name": name})},
        files={"file": ("skill.zip", buffer.getvalue(), "application/zip")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending_review"
    path: Final = f"/claude-code/skill-submissions/{response.json()['submission_id']}/archive"
    owned: Final = await submission_client.get(path)
    viewer: Final = await submission_client.get(path, headers={"x-test-user": "viewer"})
    assert (owned.status_code, viewer.status_code, owned.content == viewer.content) == (200, 200, True)
    with zipfile.ZipFile(io.BytesIO(owned.content)) as sanitized:
        assert sanitized.read(f"skills/{name}/SKILL.md") == b"Private proposal instructions"
    assert (await submission_client.get(path, headers={"x-test-user": "other"})).status_code == 404
    assert (await submission_client.get(response.json()["plugin"]["source"]["url"])).status_code == 404
    assert await _listed_names(_USER) == ({name} if updating else set())


@pytest.mark.asyncio
async def test_write_authorization_precedes_missing_database() -> None:
    request: Final = RegisterPluginRequest(name="no-database", source=_GIT_SUBDIR_SOURCE)
    with pytest.raises(HTTPException) as unavailable:
        await register_plugin_route(request, uploads=None, submissions=None, user_api_key_dict=_NON_ADMIN_USER)
    assert unavailable.value.status_code == 500
    with pytest.raises(HTTPException) as forbidden:
        await claude_code_marketplace.approve_skill_submission(
            "submission-id",
            submissions=None,
            user_api_key_dict=_NON_ADMIN_USER,
        )
    assert forbidden.value.status_code == 403


def test_submission_routes_admit_members_as_management_without_inference_classification() -> None:
    from starlette.requests import Request

    from litellm.proxy.auth.route_checks import RouteChecks

    for method, path in (
        ("GET", "/claude-code/skill-submissions"),
        ("GET", "/claude-code/skill-submissions/submission-id"),
        ("GET", "/claude-code/skill-submissions/submission-id/archive"),
        ("PUT", "/claude-code/skill-submissions/submission-id/approve"),
        ("PUT", "/claude-code/skill-submissions/submission-id/reject"),
        ("PUT", "/claude-code/plugins/my-skill/upload"),
    ):
        RouteChecks.non_proxy_admin_allowed_routes_check(
            user_obj=None,
            _user_role=LitellmUserRoles.INTERNAL_USER,
            route=path,
            request=Request({"type": "http", "method": method, "path": path, "headers": []}),
            valid_token=_NON_ADMIN_USER,
            request_data={},
        )
        assert not RouteChecks.is_llm_api_route(route=path), path


@pytest.mark.asyncio
async def test_granted_private_update_keeps_visibility_after_approval(submission_client: httpx.AsyncClient) -> None:
    await _register_public_and_private_plugins()
    proposal: Final = await update_plugin(
        plugin_name="private-skill",
        request=UpdatePluginRequest(source={"source": "github", "repo": "org/private-replacement"}),
        user_api_key_dict=_granted_user(["private-skill"]),
    )
    path: Final = f"/claude-code/skill-submissions/{proposal.submission_id}"
    detail: Final = await submission_client.get(path, headers={"x-test-user": "viewer"})
    assert detail.json()["current_plugin"]["enabled"] is False
    approved: Final = await submission_client.put(f"{path}/approve", headers={"x-test-user": "admin"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["plugin"]["enabled"] is False
    assert await _listed_names(_NON_ADMIN_USER) == {"public-skill"}
    assert await _listed_names(_granted_user(["private-skill"])) == {"public-skill", "private-skill"}


def _uploads() -> PluginUploadRepository:
    return PluginUploadRepository(litellm.proxy.proxy_server.prisma_client)


@pytest.mark.asyncio
async def test_published_updates_auto_bump_and_reject_stale_editors() -> None:
    from datetime import datetime, timezone

    source: Final = {"source": "github", "repo": "example/skill"}
    created: Final = await register_plugin(
        request=RegisterPluginRequest(name="versioned", source=source, version=None),
        user_api_key_dict=_USER,
    )
    updated: Final = await update_plugin(
        plugin_name="versioned", request=UpdatePluginRequest(source=source),
        user_api_key_dict=_USER,
    )
    assert (created.plugin.version, updated.plugin.version) == ("1.0.0", "1.0.1")
    with pytest.raises(HTTPException) as stale:
        await update_plugin(
            plugin_name="versioned", user_api_key_dict=_USER,
            request=UpdatePluginRequest(source=source, expected_updated_at=datetime(2000, 1, 1, tzinfo=timezone.utc)),
        )
    assert stale.value.status_code == 409
    assert (await _read_stored_manifest("versioned"))["version"] == "1.0.1"


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["0.9.0", "1.0.0", "1.0.0+build", "invalid"])
async def test_rejected_version_leaves_published_metadata_unchanged(version: str) -> None:
    source: Final = {"source": "github", "repo": "example/skill"}
    await register_plugin(
        request=RegisterPluginRequest(name="versioned", source=source), user_api_key_dict=_USER,
    )
    before: Final = await _read_stored_manifest("versioned")
    with pytest.raises(HTTPException) as error:
        await update_plugin(
            plugin_name="versioned", user_api_key_dict=_USER,
            request=UpdatePluginRequest(source=source, version=version, description="rejected edit"),
        )
    assert error.value.status_code == 400
    assert await _read_stored_manifest("versioned") == before


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_identity", [False, True])
async def test_publication_rejects_a_changed_base_inside_the_transaction(changed_identity: bool) -> None:
    from datetime import datetime, timezone

    from litellm.models.plugin_upload import PluginPublication, PluginPublicationBase, PluginPublicationError

    source: Final = {"source": "github", "repo": "example/skill"}
    await register_plugin(
        request=RegisterPluginRequest(name="locked", source=source), user_api_key_dict=_USER,
    )
    table: Final = litellm.proxy.proxy_server.prisma_client.writer_db.litellm_claudecodeplugintable
    current: Final = await table.find_unique(where={"name": "locked"})
    before: Final = await _read_stored_manifest("locked")
    result: Final = await _uploads().publish(
        PluginPublication("locked", None, "stale edit", {"source": source}, "editor"),
        PluginPublicationBase("old-id" if changed_identity else current.id,
                              current.updated_at if changed_identity else datetime(2000, 1, 1, tzinfo=timezone.utc)),
    )
    assert isinstance(result, PluginPublicationError)
    assert result.kind == "conflict"
    assert await _read_stored_manifest("locked") == before


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,key,missing,status", [
    (True, None, False, 401),
    (True, "sk-plain", False, 200),
    (False, "sk-plain", False, 403),
    (False, _LIMITED_KEY, False, 200),
    (True, _LIMITED_KEY, True, 404),
])
async def test_history_uses_current_skill_visibility(
    upload_storage: _UploadClient, enabled: bool, key: str | None, missing: bool, status: int,
) -> None:
    client, plugins, _ = upload_storage
    plugins.find_unique.return_value = None if missing else plugins.find_unique.return_value.model_copy(
        update={"enabled": enabled},
    )
    response: Final = await client.get(
        f"/claude-code/plugins/{_UPLOAD_NAME}/versions",
        headers={"Authorization": f"Bearer {key}"} if key else {},
    )
    assert response.status_code == status, response.text
    if status == 200:
        assert response.json() == {"versions": [], "next_offset": None}


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 20])
async def test_history_paginates_metadata_and_only_links_retained_archives(
    upload_storage: _UploadClient, limit: int,
) -> None:
    client, _, _ = upload_storage
    database: Final = litellm.proxy.proxy_server.prisma_client.writer_db
    database.query_raw.return_value = [
        {
            "id": "new", "version": "1.0.1", "description": "remote reference",
            "manifest": {"source": {"source": "github", "repo": "example/current"}},
            "published_at": None, "published_by": "admin", "archive_available": False,
        },
        {
            "id": "old", "version": "1.0.0", "description": "retained upload",
            "manifest": {"source": _UPLOAD_SOURCE},
            "published_at": None, "published_by": "admin", "archive_available": True,
        },
    ]
    response: Final = await client.get(
        f"/claude-code/plugins/{_UPLOAD_NAME}/versions?limit={limit}", headers={"Authorization": f"Bearer {MASTER_KEY}"},
    )
    assert response.status_code == 200, response.text
    body: Final = response.json()
    assert body["next_offset"] == (1 if limit == 1 else None)
    assert len(body["versions"]) == min(limit, 2)
    assert body["versions"][0]["content_kind"] == "source_reference"
    assert body["versions"][0]["archive_path"] is None
    if limit > 1:
        assert body["versions"][1]["archive_path"] == f"/claude-code/plugins/{_UPLOAD_NAME}/versions/old/archive"
        assert body["versions"][1]["content_kind"] == "stored_archive"


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,key,content,status", [
    (True, None, b"stored archive", 401),
    (False, "sk-plain", b"stored archive", 404),
    (False, _LIMITED_KEY, b"stored archive", 200),
    (True, "sk-plain", b"stored archive", 200),
    (True, "sk-plain", b"changed bytes", 404),
    (True, "sk-plain", None, 404),
])
async def test_historical_download_requires_current_access_and_verified_bytes(
    upload_storage: _UploadClient, enabled: bool, key: str | None, content: bytes | None, status: int,
) -> None:
    client, plugins, _ = upload_storage
    plugins.find_unique.return_value = plugins.find_unique.return_value.model_copy(update={"enabled": enabled})
    database: Final = litellm.proxy.proxy_server.prisma_client.writer_db
    database.litellm_claudecodepluginversion.find_first = AsyncMock(return_value=prisma_models.LiteLLM_ClaudeCodePluginVersion(
        id="retained", plugin_id="uploaded-id", archive_available=content is not None,
        manifest_json=json.dumps({"source": _UPLOAD_SOURCE}),
        content=Base64.encode(content) if content is not None else None,
    ))
    response: Final = await client.get(
        f"/claude-code/plugins/{_UPLOAD_NAME}/versions/retained/archive",
        headers={"Authorization": f"Bearer {key}"} if key else {},
    )
    assert response.status_code == status, response.text
    if status == 200:
        assert response.content == b"stored archive"
        assert response.headers["cache-control"] == "private, no-store"
    else:
        assert b"stored archive" not in response.content


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["url-create", "url-update", "zip-create", "zip-update"])
async def test_member_invalid_version_never_stages_or_publishes(
    submission_client: httpx.AsyncClient,
    mode: str,
) -> None:
    updating: Final = mode.endswith("update")
    name: Final = "version-proposal"
    if updating:
        await register_plugin(
            request=RegisterPluginRequest(name=name, source=_GIT_SUBDIR_SOURCE), user_api_key_dict=_USER
        )
    database: Final = litellm.proxy.proxy_server.prisma_client.writer_db
    history_before: Final = database.litellm_claudecodepluginversion.create.await_count
    path: Final = f"/claude-code/plugins/{name}" if updating else "/claude-code/plugins"
    metadata: Final = {"version": "invalid", **({} if updating else {"name": name})}
    buffer: Final = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("SKILL.md", "Proposed instructions")
    response: Final = (
        await submission_client.request(
            "PUT" if updating else "POST",
            f"{path}/upload",
            data={"metadata": json.dumps(metadata)},
            files={"file": ("skill.zip", buffer.getvalue(), "application/zip")},
        )
        if mode.startswith("zip")
        else await submission_client.request(
            "PUT" if updating else "POST",
            path,
            json={**metadata, "source": _GIT_SUBDIR_SOURCE},
        )
    )
    assert response.status_code == 400, response.text
    assert (await submission_client.get("/claude-code/skill-submissions")).json() == {"submissions": [], "count": 0}
    assert await _listed_names(_USER) == ({name} if updating else set())
    assert database.litellm_claudecodepluginversion.create.await_count == history_before


@pytest.mark.asyncio
async def test_pending_auto_version_is_resolved_only_when_approved(submission_client: httpx.AsyncClient) -> None:
    name: Final = "automatic-proposal"
    await register_plugin(request=RegisterPluginRequest(name=name, source=_GIT_SUBDIR_SOURCE), user_api_key_dict=_USER)
    database: Final = litellm.proxy.proxy_server.prisma_client.writer_db
    history_before: Final = database.litellm_claudecodepluginversion.create.await_count
    response: Final = await submission_client.put(f"/claude-code/plugins/{name}", json={"source": _GIT_SUBDIR_SOURCE})
    assert response.status_code == 200, response.text
    path: Final = f"/claude-code/skill-submissions/{response.json()['submission_id']}"
    detail: Final = await submission_client.get(path)
    assert detail.json()["submission"]["proposal"]["version"] is None
    assert (await _read_stored_manifest(name))["version"] == "1.0.0"
    assert database.litellm_claudecodepluginversion.create.await_count == history_before
    approved: Final = await submission_client.put(f"{path}/approve", headers={"x-test-user": "admin"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["plugin"]["version"] == "1.0.1"
    assert approved.json()["submission"]["proposal"]["version"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_target, expected_status", [(False, 400), (True, 409)])
async def test_failed_publication_keeps_pending_review_and_typed_error(
    submission_client: httpx.AsyncClient,
    missing_target: bool,
    expected_status: int,
) -> None:
    from litellm.models.plugin_upload import PluginPublication, PluginPublicationBase

    database: Final = litellm.proxy.proxy_server.prisma_client
    submission: Final = await SkillSubmissionRepository(database).submit(
        PluginPublication(
            name="older-proposal",
            version=None if missing_target else "invalid",
            description=None,
            manifest={"name": "older-proposal", "source": _GIT_SUBDIR_SOURCE},
            created_by="regular-user",
        ),
        submitted_by="regular-user",
        base=PluginPublicationBase("deleted-plugin", datetime(2026, 1, 1, tzinfo=timezone.utc))
        if missing_target
        else None,
    )
    path: Final = f"/claude-code/skill-submissions/{submission.id}"
    response: Final = await submission_client.put(f"{path}/approve", headers={"x-test-user": "admin"})
    assert response.status_code == expected_status, response.text
    assert (await submission_client.get(path)).json()["submission"]["status"] == "pending_review"
    assert (await list_plugins(user_api_key_dict=_USER)).plugins == []
    database.writer_db.litellm_claudecodepluginversion.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_history_routes_keep_current_skill_visibility(submission_client: httpx.AsyncClient) -> None:
    await _register_public_and_private_plugins()
    public: Final = await submission_client.get("/claude-code/plugins/public-skill/versions")
    private: Final = await submission_client.get("/claude-code/plugins/private-skill/versions")
    private_archive: Final = await submission_client.get("/claude-code/plugins/private-skill/versions/old/archive")
    assert (public.status_code, public.json()) == (200, {"versions": [], "next_offset": None})
    assert private.status_code == 403, private.text
    assert private_archive.status_code == 404, private_archive.text


@pytest.mark.asyncio
async def test_review_snapshot_tolerates_legacy_installation_preference(submission_client: httpx.AsyncClient) -> None:
    name: Final = "legacy-preference"
    await register_plugin(request=RegisterPluginRequest(name=name, source=_GIT_SUBDIR_SOURCE), user_api_key_dict=_USER)
    table: Final = litellm.proxy.proxy_server.prisma_client.writer_db.litellm_claudecodeplugintable
    await table.update(
        where={"name": name},
        data={"manifest_json": json.dumps({"source": _GIT_SUBDIR_SOURCE, "installation_preference": "auto-install"})},
    )
    proposed: Final = await submission_client.put(f"/claude-code/plugins/{name}", json={"source": _GIT_SUBDIR_SOURCE})
    assert proposed.status_code == 200, proposed.text
    detail: Final = await submission_client.get(
        f"/claude-code/skill-submissions/{proposed.json()['submission_id']}",
        headers={"x-test-user": "viewer"},
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["current_plugin"]["installation_preference"] is None
    assert detail.json()["current_plugin"]["source"] == _GIT_SUBDIR_SOURCE
