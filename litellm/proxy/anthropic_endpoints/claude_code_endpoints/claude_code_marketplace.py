"""
CLAUDE CODE MARKETPLACE

Provides a registry/discovery layer for Claude Code plugins.
Plugins are stored as metadata + source references in LiteLLM database.
Actual plugin files are hosted on GitHub/GitLab/Bitbucket or as a zip archive on
any HTTPS host (S3, Artifactory, a static file server).

Endpoints:
/claude-code/marketplace.json  - GET  - List plugins for Claude Code discovery (anonymous unless
                                 general_settings.claude_code_marketplace_auth_required; a key adds its granted skills)
/claude-code/plugins           - POST - Register a new plugin (members submit for review, admins publish)
/claude-code/plugins           - GET  - List plugins visible to the key (enabled, plus granted disabled ones)
/claude-code/plugins/{name}    - GET  - Get plugin details (403 on a disabled plugin the key is not granted)
/claude-code/plugins/{name}    - PUT  - Replace a plugin (members submit for review, admins publish)
/claude-code/plugins/{name}/enable  - POST - Enable a plugin (proxy admin only)
/claude-code/plugins/{name}/disable - POST - Disable a plugin (proxy admin only)
/claude-code/plugins/{name}    - DELETE - Delete a plugin (proxy admin only)
"""

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Annotated, Final, Protocol, TypedDict, assert_never, get_args

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import Json, TypeAdapter
from typing_extensions import ReadOnly

from litellm._logging import verbose_proxy_logger
from litellm.litellm_core_utils.url_utils import is_https_url_with_host
from litellm.models.plugin_upload import (
    PluginPublication,
    PluginPublicationBase,
    PluginPublicationError,
    PluginSnapshot,
    PublishedPlugin,
)
from litellm.models.skill_submission import (
    SkillSubmission,
    SubmissionConflict,
    SubmissionMissing,
    SubmissionReviewed,
    SubmissionStatus,
)
from litellm.proxy._types import CommonProxyErrors, ProxyErrorTypes, ProxyException, UserAPIKeyAuth
from litellm.proxy.anthropic_endpoints.claude_code_endpoints.claude_code_skill_access import (
    SkillVisibility,
    resolve_skill_visibility,
)
from litellm.proxy.anthropic_endpoints.claude_code_endpoints.skill_upload_archive import (
    MAX_SKILL_UPLOAD_BYTES,
    build_uploaded_skill_archive,
)
from litellm.proxy.auth.auth_exception_handler import DB_UNAVAILABLE_FALLBACK_USER_ID
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.common_utils.claude_code_marketplace_auth import (
    marketplace_auth_required,
    marketplace_key_header_present,
)
from litellm.proxy.common_utils.resource_ownership import is_proxy_admin
from litellm.proxy.deps.repositories import PluginUploadRepositoryDep, SkillSubmissionRepositoryDep
from litellm.repositories.plugin_publication import resolve_plugin_version
from litellm.repositories.table_repositories import ClaudeCodePluginRepository
from litellm.types.proxy.auth.special_headers import SpecialHeaders
from litellm.types.proxy.auth.user_roles import LitellmUserRoles
from litellm.types.proxy.claude_code_endpoints import (
    InstallationPreference,
    ListPluginsResponse,
    ListPluginVersionsResponse,
    ListSkillSubmissionsResponse,
    PluginListItem,
    PluginResponse,
    PluginSpec,
    PluginVersionResponse,
    RegisterPluginRequest,
    RegisterPluginResponse,
    RejectSkillSubmissionRequest,
    ReviewSkillSubmissionResponse,
    SkillSubmissionDetail,
    SkillSubmissionResponse,
    UpdatePluginRequest,
    UpdateUploadedPluginRequest,
    UploadPluginRequest,
)

router: Final = APIRouter()


class _PluginRecord(Protocol):
    id: str
    name: str
    version: str | None
    description: str | None
    manifest_json: str | None
    enabled: bool
    created_at: datetime | None
    updated_at: datetime | None
    created_by: str | None


class _MarketplaceEntry(TypedDict, total=False):
    name: str
    source: object
    version: str
    description: str
    author: object
    homepage: object
    keywords: object
    category: object
    installationPreference: ReadOnly[InstallationPreference]


_INSTALLATION_PREFERENCES: Final[tuple[InstallationPreference, ...]] = get_args(InstallationPreference)
MANIFEST_FIELDS: Final = TypeAdapter(dict[str, object])


_SELF_INSTALLING_PREFERENCES: Final[frozenset[InstallationPreference]] = frozenset({"auto_install", "required"})


def get_installation_preference(manifest: Mapping[str, object]) -> InstallationPreference | None:
    value: Final = manifest.get("installation_preference")
    if value is None or value in _INSTALLATION_PREFERENCES:
        return value
    verbose_proxy_logger.warning(
        "Plugin %s has unknown installation_preference %r, serving it as unset", manifest.get("name"), value
    )
    return None


async def get_prisma_client() -> object:
    """Get the prisma client from proxy_server."""
    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        raise HTTPException(
            status_code=500,
            detail={"error": CommonProxyErrors.db_not_connected_error.value},
        )
    return prisma_client


_get_prisma_client: Final = get_prisma_client


async def _header_key_caller(request: Request) -> UserAPIKeyAuth:
    try:
        caller: Final = await user_api_key_auth(
            request=request,
            api_key=request.headers.get(SpecialHeaders.openai_authorization.value) or "",
            custom_litellm_key_header=request.headers.get(SpecialHeaders.custom_litellm_api_key.value),
        )
    except ProxyException as e:
        if e.code != "401":
            raise
        raise ProxyException(
            message=e.message,
            type=e.type,
            param=e.param,
            code=401,
            headers={**e.headers, "WWW-Authenticate": "Bearer"},
        ) from e
    if caller.user_id == DB_UNAVAILABLE_FALLBACK_USER_ID:
        raise ProxyException(
            message="The LiteLLM key could not be verified because the database is unavailable",
            type=ProxyErrorTypes.no_db_connection,
            param=None,
            code=503,
        )
    return caller


async def _marketplace_caller(request: Request, key: str | None) -> UserAPIKeyAuth | None:
    if marketplace_auth_required():
        return await _header_key_caller(request)
    if key:
        return await user_api_key_auth(request=request, api_key=f"Bearer {key}")
    return None


async def _uploaded_spec(
    request: Request, file: UploadFile, name: str, metadata: UploadPluginRequest | UpdateUploadedPluginRequest
) -> tuple[RegisterPluginRequest, bytes]:
    from litellm.proxy.utils import get_custom_url

    content: Final = await file.read(MAX_SKILL_UPLOAD_BYTES + 1)
    if len(content) > MAX_SKILL_UPLOAD_BYTES:
        raise _error_response(413, "Skill ZIP must be 10 MiB or smaller")
    archive: Final = await asyncio.to_thread(build_uploaded_skill_archive, content, name)
    if archive is None:
        raise _error_response(
            400, "Upload a valid ZIP with SKILL.md at the root or inside one skill folder, and safe supporting files"
        )
    digest: Final = hashlib.sha256(archive).hexdigest()
    source: Final = {
        "source": "archive",
        "url": get_custom_url(
            request_base_url=str(request.base_url),
            route=f"claude-code/plugins/{name}/archive/{digest}.zip",
        ),
        "sha256": digest,
    }
    return RegisterPluginRequest.model_validate({**metadata.model_dump(), "name": name, "source": source}), archive


@router.post(
    "/claude-code/plugins/upload",
    tags=["Claude Code Marketplace"],
    response_model=RegisterPluginResponse,
)
async def upload_plugin(
    request: Request,
    uploads: PluginUploadRepositoryDep,
    submissions: SkillSubmissionRepositoryDep,
    user_api_key_dict: Annotated[UserAPIKeyAuth, Depends(user_api_key_auth)],
    file: Annotated[UploadFile, File(description="Skill ZIP containing SKILL.md and supporting files, up to 10 MiB")],
    metadata: Annotated[Json[UploadPluginRequest], Form(description="JSON skill metadata without a source URL")],
) -> RegisterPluginResponse:
    """Submit a sanitized skill ZIP for review, or publish it directly as a proxy admin. Names are create-only."""
    _require_skill_writer(user_api_key_dict)
    if uploads is None:
        raise _error_response(500, CommonProxyErrors.db_not_connected_error.value)
    spec, archive = await _uploaded_spec(request, file, metadata.name, metadata)
    return await _write_skill(spec.name, spec, archive, False, uploads, submissions, user_api_key_dict)


@router.put(
    "/claude-code/plugins/{plugin_name}/upload",
    tags=["Claude Code Marketplace"],
    response_model=RegisterPluginResponse,
)
async def update_uploaded_plugin(
    plugin_name: str,
    request: Request,
    uploads: PluginUploadRepositoryDep,
    submissions: SkillSubmissionRepositoryDep,
    user_api_key_dict: Annotated[UserAPIKeyAuth, Depends(user_api_key_auth)],
    file: Annotated[UploadFile, File(description="Replacement skill ZIP containing SKILL.md, up to 10 MiB")],
    metadata: Annotated[
        Json[UpdateUploadedPluginRequest], Form(description="Full replacement metadata without name/source")
    ],
) -> RegisterPluginResponse:
    """Replace a visible skill with a sanitized ZIP. Members submit for review and proxy admins publish atomically."""
    _require_skill_writer(user_api_key_dict)
    if uploads is None:
        raise _error_response(500, CommonProxyErrors.db_not_connected_error.value)
    spec, archive = await _uploaded_spec(request, file, plugin_name, metadata)
    return await _write_skill(
        plugin_name, spec, archive, True, uploads, submissions, user_api_key_dict, metadata.expected_updated_at
    )


@router.get(
    "/claude-code/plugins/{plugin_name}/archive/{digest}.zip",
    tags=["Claude Code Marketplace"],
    response_class=Response,
    responses={200: {"content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}}}},
)
async def download_plugin_archive(
    request: Request,
    plugin_name: str,
    digest: str,
    uploads: PluginUploadRepositoryDep,
) -> Response:
    """Download an uploaded ZIP under the same visibility policy as its skill."""
    from litellm.proxy.utils import get_custom_url

    caller: Final = (
        await _header_key_caller(request)
        if marketplace_key_header_present(request)
        else await _marketplace_caller(request, None)
    )
    visibility: Final = await resolve_skill_visibility(caller)
    if uploads is None:
        raise _error_response(500, CommonProxyErrors.db_not_connected_error.value)
    archive: Final = await uploads.get_metadata(plugin_name)
    expected_url: Final = get_custom_url(
        request_base_url=str(request.base_url), route=f"claude-code/plugins/{plugin_name}/archive/{digest}.zip"
    )
    if (
        archive is None
        or not visibility.allows(archive)
        or archive.source != {"source": "archive", "url": expected_url, "sha256": digest}
    ):
        raise _error_response(404, "Skill archive not found")
    content: Final = await uploads.get_content(archive)
    if content is None:
        raise _error_response(404, "Skill archive not found")
    return Response(
        content=content,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{archive.name}.zip"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get(
    "/claude-code/marketplace.json",
    tags=["Claude Code Marketplace"],
)
async def get_marketplace(request: Request, key: str | None = None):
    """
    Serve marketplace.json for Claude Code plugin discovery.

    This endpoint is accessed by Claude Code CLI when users run:
    - claude plugin marketplace add <url>
    - claude plugin install <name>@<marketplace>

    Without a key the catalog holds the enabled (public) plugins. With `?key=sk-...`
    the key is authenticated and the catalog also holds the disabled plugins granted
    to it through `object_permission.skills` or an access group on the key or its team.

    With `general_settings.claude_code_marketplace_auth_required: true` the key must come
    in the `Authorization` or `x-litellm-api-key` header (Claude Code sends them from the
    marketplace source's `headers`), `?key=` is ignored, and a caller with no valid key
    gets a 401 with `WWW-Authenticate: Bearer`.

    Returns:
        Marketplace catalog with list of available plugins and their git sources.

    Example:
        ```bash
        claude plugin marketplace add http://localhost:4000/claude-code/marketplace.json
        claude plugin marketplace add "http://localhost:4000/claude-code/marketplace.json?key=sk-..."
        claude plugin install my-plugin@litellm
        ```
    """
    try:
        prisma_client: Final = await get_prisma_client()
        caller: Final = await _marketplace_caller(request, key)
        visibility: Final[SkillVisibility] = await resolve_skill_visibility(caller)
        plugins: Final[Sequence[_PluginRecord]] = await ClaudeCodePluginRepository(prisma_client).table.find_many(
            where=visibility.where()
        )

        plugin_list: Final = []
        for plugin in plugins:
            if not visibility.allows(plugin):
                continue
            try:
                manifest: Mapping[str, object] = json.loads(plugin.manifest_json or "{}")
            except json.JSONDecodeError:
                verbose_proxy_logger.warning("Plugin %s has invalid manifest JSON, skipping", plugin.name)
                continue

            # Source must be specified for URL-based marketplaces
            if "source" not in manifest:
                verbose_proxy_logger.warning("Plugin %s has no source field, skipping", plugin.name)
                continue

            installation_preference = get_installation_preference(manifest)
            entry: _MarketplaceEntry = (
                {"name": plugin.name, "source": manifest["source"]}
                if installation_preference is None
                else {
                    "name": plugin.name,
                    "source": manifest["source"],
                    "installationPreference": installation_preference,
                }
            )

            if plugin.version:
                entry["version"] = plugin.version
            if plugin.description:
                entry["description"] = plugin.description
            if "author" in manifest:
                entry["author"] = manifest["author"]
            if "homepage" in manifest:
                entry["homepage"] = manifest["homepage"]
            if "keywords" in manifest:
                entry["keywords"] = manifest["keywords"]
            if "category" in manifest:
                entry["category"] = manifest["category"]

            plugin_list.append(entry)

        marketplace: Final = {
            "name": "litellm",
            "owner": {"name": "LiteLLM", "email": "support@litellm.ai"},
            "plugins": plugin_list,
        }

        return JSONResponse(content=marketplace)

    except (HTTPException, ProxyException):
        raise
    except Exception as e:
        verbose_proxy_logger.exception("Error generating marketplace: %s", e)
        raise HTTPException(
            status_code=500,
            detail={"error": "Failed to generate marketplace"},
        )


# Allowlist for git-subdir paths: one or more segments separated by '/'.
# Each segment must start with an alphanumeric character and contain only
# alphanumeric characters, dots, hyphens, and underscores.
# This implicitly blocks '..', leading '/', backslashes, and percent-encoded sequences.
_VALID_GIT_SUBDIR_PATH_RE: Final = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*(/[a-zA-Z0-9][a-zA-Z0-9._-]*)*$")
_VALID_SHA256_RE: Final = re.compile(r"^[0-9a-fA-F]{64}$")


def _validate_plugin_source(source: Mapping[str, str]) -> None:
    """Validate plugin source format, raising HTTPException on invalid input."""
    source_type: Final = source.get("source")
    if source_type == "github":
        if "repo" not in source:
            raise HTTPException(
                status_code=400,
                detail={"error": "GitHub source must include 'repo' field (e.g., 'org/repo')"},
            )
    elif source_type == "url":
        if "url" not in source:
            raise HTTPException(
                status_code=400,
                detail={"error": "URL source must include 'url' field (e.g., 'https://github.com/org/repo.git')"},
            )
    elif source_type == "git-subdir":
        if not source.get("url"):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "git-subdir source must include 'url' field (e.g., 'https://github.com/org/repo.git')"
                },
            )
        if not source.get("path"):
            raise HTTPException(
                status_code=400,
                detail={"error": "git-subdir source must include 'path' field (e.g., 'plugins/plugin-name')"},
            )
        if not _VALID_GIT_SUBDIR_PATH_RE.match(source["path"]):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "git-subdir 'path' must be a relative path of the form 'segment/segment' (alphanumeric, dots, hyphens, underscores only)"
                },
            )
    elif source_type == "archive":
        if not is_https_url_with_host(source.get("url", "")):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "archive source must include an https 'url' field "
                    "(e.g., 'https://bucket.s3.amazonaws.com/plugins/plugin-name.zip')"
                },
            )
        if "sha256" in source and not _VALID_SHA256_RE.match(source["sha256"]):
            raise HTTPException(
                status_code=400,
                detail={"error": "archive 'sha256' must be a 64-character hex digest"},
            )
    else:
        raise HTTPException(
            status_code=400,
            detail={"error": "source.source must be 'github', 'url', 'git-subdir', or 'archive'"},
        )


def _validate_installation_preference(spec: PluginSpec) -> None:
    if spec.installation_preference is None:
        return
    if spec.source.get("source") != "archive":
        raise HTTPException(
            status_code=400,
            detail={
                "error": "installation_preference needs an archive source, the only plugins Claude Desktop lists "
                "from a marketplace.json URL"
            },
        )
    if spec.installation_preference in _SELF_INSTALLING_PREFERENCES and not spec.source.get("sha256"):
        raise HTTPException(
            status_code=400,
            detail={
                "error": f"installation_preference '{spec.installation_preference}' needs the archive's sha256, "
                "since Claude Desktop installs only pinned archives on its own"
            },
        )


def _build_plugin_manifest(name: str, spec: PluginSpec) -> Mapping[str, object]:
    """Build the stored manifest dict shared by plugin create and update."""
    dumped: Final[Mapping[str, object]] = MappingProxyType(spec.model_dump(exclude_none=True))
    return {"name": name, **{key: value for key, value in dumped.items() if value and key != "name"}}


def _error_response(status_code: int, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"error": message})


def _publication_error(error: PluginPublicationError) -> HTTPException:
    match error.kind:
        case "invalid_version":
            return _error_response(400, error.message)
        case "not_found":
            return _error_response(404, error.message)
        case "conflict":
            return _error_response(409, error.message)


def _name_conflict_error(name: str) -> HTTPException:
    return _error_response(
        409, f"A skill named '{name}' already exists. Update the existing skill instead of adding it again."
    )


def _require_proxy_admin(user_api_key_dict: UserAPIKeyAuth) -> None:
    """Catalog mutations are restricted to proxy admins: marketplace.json is served
    unauthenticated and any registered/updated entry is immediately installable by
    every user, so a non-admin key must never be able to add or overwrite one.
    """
    if not is_proxy_admin(user_api_key_dict):
        raise HTTPException(
            status_code=403,
            detail={"error": "Only proxy admins may modify the Claude Code plugin marketplace."},
        )


def _require_skill_writer(caller: UserAPIKeyAuth) -> None:
    if is_proxy_admin(caller):
        return
    if (
        not caller.user_id
        or caller.user_id == DB_UNAVAILABLE_FALLBACK_USER_ID
        or caller.user_role in (None, LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY, LitellmUserRoles.INTERNAL_USER_VIEW_ONLY)
    ):
        raise _error_response(403, "Submitting a skill requires a writable user identity.")


def _plugin_response(plugin: PublishedPlugin, spec: PluginSpec) -> PluginResponse:
    return PluginResponse(
        id=plugin.id,
        name=plugin.name,
        version=plugin.version,
        description=spec.description,
        source=spec.source,
        installation_preference=spec.installation_preference,
        enabled=plugin.enabled,
    )


async def _write_skill(
    name: str,
    spec: PluginSpec,
    content: bytes | None,
    updating: bool,
    uploads: PluginUploadRepositoryDep,
    submissions: SkillSubmissionRepositoryDep,
    caller: UserAPIKeyAuth,
    expected_updated_at: datetime | None = None,
) -> RegisterPluginResponse:
    from prisma.errors import PrismaError

    _require_skill_writer(caller)
    if uploads is None or (not is_proxy_admin(caller) and submissions is None):
        raise _error_response(500, CommonProxyErrors.db_not_connected_error.value)
    try:
        existing: Final = await uploads.get_plugin(name)
        if updating and (existing is None or not (await resolve_skill_visibility(caller)).allows(existing)):
            raise _error_response(404, "Skill not found")
        if existing is not None and expected_updated_at is not None and existing.updated_at != expected_updated_at:
            raise _error_response(409, "The published skill changed. Reload it before submitting.")
        if not updating and existing is not None:
            raise _name_conflict_error(name)
        if content is None and (existing is None or spec.source != existing.manifest.get("source")):
            _validate_plugin_source(spec.source)
        _validate_installation_preference(spec)
        version_validation: Final = resolve_plugin_version(
            spec.version, None if existing is None else existing.version, updating=updating
        )
        if isinstance(version_validation, PluginPublicationError):
            raise _publication_error(version_validation)
        upload: Final = PluginPublication(
            name=name,
            version=spec.version,
            description=spec.description,
            manifest=_build_plugin_manifest(name, spec),
            content=content,
            created_by=caller.user_id,
        )
        expected: Final = None if existing is None else PluginPublicationBase(existing.id, existing.updated_at)
        if not is_proxy_admin(caller):
            if submissions is None or not caller.user_id:
                raise _error_response(403, "Submitting a skill requires a writable user identity.")
            submission: Final = await submissions.submit(upload, caller.user_id, expected)
            return RegisterPluginResponse(
                status="pending_review",
                action="submitted",
                submission_id=submission.id,
                plugin=PluginResponse(
                    id=submission.id,
                    name=name,
                    version=spec.version,
                    description=spec.description,
                    source=spec.source,
                    installation_preference=spec.installation_preference,
                    enabled=False,
                ),
            )
        result: Final = await uploads.publish(upload, expected)
        if isinstance(result, PluginPublicationError):
            raise _publication_error(result)
        return RegisterPluginResponse(
            status="success",
            action="updated" if updating else "created",
            plugin=_plugin_response(result, spec),
            submission_id=None,
        )
    except PrismaError:
        verbose_proxy_logger.exception("Error writing skill %s", name)
        raise _error_response(500, "Skill write failed")


@router.post(
    "/claude-code/plugins",
    tags=["Claude Code Marketplace"],
    response_model=RegisterPluginResponse,
)
async def register_plugin(
    request: RegisterPluginRequest,
    uploads: PluginUploadRepositoryDep,
    submissions: SkillSubmissionRepositoryDep,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> RegisterPluginResponse:
    """Register a new skill. Members submit an immutable proposal; proxy admins publish directly. Duplicate names return 409."""
    return await _write_skill(request.name, request, None, False, uploads, submissions, user_api_key_dict)


@router.get(
    "/claude-code/plugins",
    tags=["Claude Code Marketplace"],
    dependencies=[Depends(user_api_key_auth)],
    response_model=ListPluginsResponse,
)
async def list_plugins(
    enabled_only: bool = False,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    """
    List all plugins in the marketplace.

    Parameters:
        - enabled_only: If true, only return enabled plugins

    Returns:
        List of plugins with their metadata.
    """
    try:
        prisma_client: Final = await get_prisma_client()

        visibility: Final[SkillVisibility] = await resolve_skill_visibility(user_api_key_dict)
        plugins: Final[Sequence[_PluginRecord]] = await ClaudeCodePluginRepository(prisma_client).table.find_many(
            where={"enabled": True} if enabled_only else visibility.where()
        )

        plugin_list: Final = []
        for p in plugins:
            if not visibility.allows(p):
                continue
            # Parse manifest to get additional fields
            manifest = json.loads(p.manifest_json) if p.manifest_json else {}

            plugin_list.append(
                PluginListItem(
                    id=p.id,
                    name=p.name,
                    version=p.version,
                    description=p.description,
                    source=manifest.get("source", {}),
                    author=manifest.get("author"),
                    homepage=manifest.get("homepage"),
                    keywords=manifest.get("keywords"),
                    category=manifest.get("category"),
                    domain=manifest.get("domain"),
                    namespace=manifest.get("namespace"),
                    installation_preference=get_installation_preference(MANIFEST_FIELDS.validate_python(manifest)),
                    enabled=p.enabled,
                    created_at=p.created_at.isoformat() if p.created_at else None,
                    updated_at=p.updated_at.isoformat() if p.updated_at else None,
                )
            )

        # Sort by created_at descending (newest first)
        plugin_list.sort(key=lambda x: x.created_at or "", reverse=True)

        return ListPluginsResponse(
            plugins=plugin_list,
            count=len(plugin_list),
        )

    except HTTPException:
        raise
    except Exception as e:
        verbose_proxy_logger.exception("Error listing plugins: %s", e)
        raise HTTPException(
            status_code=500,
            detail={"error": str(e)},
        )


@router.get(
    "/claude-code/plugins/{plugin_name}",
    tags=["Claude Code Marketplace"],
    dependencies=[Depends(user_api_key_auth)],
)
async def get_plugin(
    plugin_name: str,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    """
    Get details of a specific plugin.

    Parameters:
        - plugin_name: The name of the plugin

    Returns:
        Plugin details including source and metadata.
    """
    try:
        prisma_client: Final = await get_prisma_client()

        plugin: Final[_PluginRecord | None] = await ClaudeCodePluginRepository(prisma_client).table.find_unique(
            where={"name": plugin_name}
        )

        if not plugin:
            raise HTTPException(
                status_code=404,
                detail={"error": f"Plugin '{plugin_name}' not found"},
            )

        if not (await resolve_skill_visibility(user_api_key_dict)).allows(plugin):
            raise HTTPException(
                status_code=403,
                detail={"error": f"Plugin '{plugin_name}' is not granted to this key"},
            )

        manifest: Final[Mapping[str, object]] = json.loads(plugin.manifest_json or "{}") if plugin.manifest_json else {}

        return {
            "id": plugin.id,
            "name": plugin.name,
            "version": plugin.version,
            "description": plugin.description,
            "source": manifest.get("source"),
            "author": manifest.get("author"),
            "homepage": manifest.get("homepage"),
            "keywords": manifest.get("keywords"),
            "category": manifest.get("category"),
            "domain": manifest.get("domain"),
            "namespace": manifest.get("namespace"),
            "installation_preference": get_installation_preference(manifest),
            "enabled": plugin.enabled,
            "created_at": plugin.created_at.isoformat() if plugin.created_at else None,
            "updated_at": plugin.updated_at.isoformat() if plugin.updated_at else None,
            "created_by": plugin.created_by,
        }

    except HTTPException:
        raise
    except Exception as e:
        verbose_proxy_logger.exception("Error getting plugin: %s", e)
        raise HTTPException(
            status_code=500,
            detail={"error": str(e)},
        )


@router.put(
    "/claude-code/plugins/{plugin_name}",
    tags=["Claude Code Marketplace"],
    response_model=RegisterPluginResponse,
)
async def update_plugin(
    plugin_name: str,
    request: UpdatePluginRequest,
    uploads: PluginUploadRepositoryDep,
    submissions: SkillSubmissionRepositoryDep,
    user_api_key_dict: Annotated[UserAPIKeyAuth, Depends(user_api_key_auth)],
) -> RegisterPluginResponse:
    """Replace visible skill metadata. An omitted version requests an automatic patch bump when published."""
    return await _write_skill(
        plugin_name, request, None, True, uploads, submissions, user_api_key_dict, request.expected_updated_at
    )


@router.post(
    "/claude-code/plugins/{plugin_name}/enable",
    tags=["Claude Code Marketplace"],
    dependencies=[Depends(user_api_key_auth)],
)
async def enable_plugin(
    plugin_name: str,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    """
    Enable a disabled plugin.

    Requires a proxy admin API key.

    Parameters:
        - plugin_name: The name of the plugin to enable
    """
    try:
        _require_proxy_admin(user_api_key_dict)

        prisma_client: Final = await get_prisma_client()

        plugin: Final[_PluginRecord | None] = await ClaudeCodePluginRepository(prisma_client).table.find_unique(
            where={"name": plugin_name}
        )
        if not plugin:
            raise HTTPException(
                status_code=404,
                detail={"error": f"Plugin '{plugin_name}' not found"},
            )

        await ClaudeCodePluginRepository(prisma_client).table.update(
            where={"name": plugin_name},
            data={"enabled": True, "updated_at": datetime.now(timezone.utc)},
        )

        verbose_proxy_logger.info("Plugin %s enabled", plugin_name)
        return {"status": "success", "message": f"Plugin '{plugin_name}' enabled"}

    except HTTPException:
        raise
    except Exception as e:
        verbose_proxy_logger.exception("Error enabling plugin: %s", e)
        raise HTTPException(
            status_code=500,
            detail={"error": str(e)},
        )


@router.post(
    "/claude-code/plugins/{plugin_name}/disable",
    tags=["Claude Code Marketplace"],
    dependencies=[Depends(user_api_key_auth)],
)
async def disable_plugin(
    plugin_name: str,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    """
    Disable a plugin without deleting it.

    Requires a proxy admin API key.

    Parameters:
        - plugin_name: The name of the plugin to disable
    """
    try:
        _require_proxy_admin(user_api_key_dict)

        prisma_client: Final = await get_prisma_client()

        plugin: Final[_PluginRecord | None] = await ClaudeCodePluginRepository(prisma_client).table.find_unique(
            where={"name": plugin_name}
        )
        if not plugin:
            raise HTTPException(
                status_code=404,
                detail={"error": f"Plugin '{plugin_name}' not found"},
            )

        await ClaudeCodePluginRepository(prisma_client).table.update(
            where={"name": plugin_name},
            data={"enabled": False, "updated_at": datetime.now(timezone.utc)},
        )

        verbose_proxy_logger.info("Plugin %s disabled", plugin_name)
        return {"status": "success", "message": f"Plugin '{plugin_name}' disabled"}

    except HTTPException:
        raise
    except Exception as e:
        verbose_proxy_logger.exception("Error disabling plugin: %s", e)
        raise HTTPException(
            status_code=500,
            detail={"error": str(e)},
        )


@router.delete(
    "/claude-code/plugins/{plugin_name}",
    tags=["Claude Code Marketplace"],
    dependencies=[Depends(user_api_key_auth)],
)
async def delete_plugin(
    plugin_name: str,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    """
    Delete a plugin from the marketplace.

    Requires a proxy admin API key.

    Parameters:
        - plugin_name: The name of the plugin to delete
    """
    try:
        _require_proxy_admin(user_api_key_dict)

        prisma_client: Final = await get_prisma_client()

        plugin: Final[_PluginRecord | None] = await ClaudeCodePluginRepository(prisma_client).table.find_unique(
            where={"name": plugin_name}
        )
        if not plugin:
            raise HTTPException(
                status_code=404,
                detail={"error": f"Plugin '{plugin_name}' not found"},
            )

        await ClaudeCodePluginRepository(prisma_client).table.delete(where={"name": plugin_name})

        verbose_proxy_logger.info("Plugin %s deleted", plugin_name)
        return {"status": "success", "message": f"Plugin '{plugin_name}' deleted"}

    except HTTPException:
        raise
    except Exception as e:
        verbose_proxy_logger.exception("Error deleting plugin: %s", e)
        raise HTTPException(
            status_code=500,
            detail={"error": str(e)},
        )


def _submission_response(submission: SkillSubmission) -> SkillSubmissionResponse:
    return SkillSubmissionResponse(
        id=submission.id,
        plugin_name=submission.plugin_name,
        operation=submission.operation,
        status=submission.status,
        proposal=submission.proposal,
        has_archive=submission.has_archive,
        submitted_by=submission.submitted_by,
        created_at=submission.created_at.isoformat(),
        base_plugin_id=submission.base_plugin_id,
        base_updated_at=None if submission.base_updated_at is None else submission.base_updated_at.isoformat(),
        reviewed_by=submission.reviewed_by,
        reviewed_at=None if submission.reviewed_at is None else submission.reviewed_at.isoformat(),
        rejection_reason=submission.rejection_reason,
    )


def _submission_owner(caller: UserAPIKeyAuth) -> str | None:
    if is_proxy_admin(caller) or caller.user_role == LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY:
        return None
    if not caller.user_id or caller.user_id == DB_UNAVAILABLE_FALLBACK_USER_ID:
        raise _error_response(403, "Reading submissions requires a user identity.")
    return caller.user_id


def _snapshot_response(plugin: PluginSnapshot) -> PluginListItem:
    manifest: Final = {
        key: value
        for key, value in plugin.manifest.items()
        if key not in ("installation_preference", "installationPreference")
    }
    spec: Final = RegisterPluginRequest.model_validate(
        {
            **manifest,
            "name": plugin.name,
            "version": plugin.version,
            "installation_preference": get_installation_preference(plugin.manifest),
        }
    )
    return PluginListItem(
        id=plugin.id,
        name=plugin.name,
        version=plugin.version,
        description=plugin.description,
        source=spec.source,
        author=spec.author,
        homepage=spec.homepage,
        keywords=spec.keywords,
        category=spec.category,
        domain=spec.domain,
        namespace=spec.namespace,
        installation_preference=spec.installation_preference,
        enabled=plugin.enabled,
        created_at=None if plugin.created_at is None else plugin.created_at.isoformat(),
        updated_at=None if plugin.updated_at is None else plugin.updated_at.isoformat(),
    )


@router.get(
    "/claude-code/skill-submissions",
    tags=["Claude Code Marketplace"],
    response_model=ListSkillSubmissionsResponse,
)
async def list_skill_submissions(
    submissions: SkillSubmissionRepositoryDep,
    user_api_key_dict: Annotated[UserAPIKeyAuth, Depends(user_api_key_auth)],
    status: SubmissionStatus | None = None,
) -> ListSkillSubmissionsResponse:
    """List the caller's immutable skill proposals. Proxy admins and admin viewers can read all proposals."""
    owner: Final = _submission_owner(user_api_key_dict)
    if submissions is None:
        raise _error_response(500, CommonProxyErrors.db_not_connected_error.value)
    rows: Final = await submissions.list(owner, status)
    return ListSkillSubmissionsResponse(submissions=tuple(_submission_response(row) for row in rows), count=len(rows))


@router.get(
    "/claude-code/skill-submissions/{submission_id}",
    tags=["Claude Code Marketplace"],
    response_model=SkillSubmissionDetail,
)
async def get_skill_submission(
    submission_id: str,
    submissions: SkillSubmissionRepositoryDep,
    uploads: PluginUploadRepositoryDep,
    user_api_key_dict: Annotated[UserAPIKeyAuth, Depends(user_api_key_auth)],
) -> SkillSubmissionDetail:
    """Read an owned proposal and the visible published skill. Admins and admin viewers have review read access."""
    owner: Final = _submission_owner(user_api_key_dict)
    if submissions is None or uploads is None:
        raise _error_response(500, CommonProxyErrors.db_not_connected_error.value)
    submission: Final = await submissions.get(submission_id, owner)
    if submission is None:
        raise _error_response(404, "Skill submission not found")
    current: Final = await uploads.get_plugin(submission.plugin_name)
    visible: Final = current is not None and (
        owner is None or (await resolve_skill_visibility(user_api_key_dict)).allows(current)
    )
    return SkillSubmissionDetail(
        submission=_submission_response(submission),
        current_plugin=_snapshot_response(current) if current is not None and visible else None,
    )


@router.get(
    "/claude-code/skill-submissions/{submission_id}/archive",
    tags=["Claude Code Marketplace"],
    response_class=Response,
    responses={200: {"content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}}}},
)
async def download_skill_submission_archive(
    submission_id: str,
    submissions: SkillSubmissionRepositoryDep,
    user_api_key_dict: Annotated[UserAPIKeyAuth, Depends(user_api_key_auth)],
) -> Response:
    """Download a proposal's sanitized ZIP privately. Only its owner, proxy admins, and admin viewers may read it."""
    owner: Final = _submission_owner(user_api_key_dict)
    if submissions is None:
        raise _error_response(500, CommonProxyErrors.db_not_connected_error.value)
    content: Final = await submissions.get_archive(submission_id, owner)
    if content is None:
        raise _error_response(404, "Skill submission archive not found")
    return Response(
        content=content,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="skill-submission.zip"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _review_skill_submission(
    submission_id: str,
    submissions: SkillSubmissionRepositoryDep,
    caller: UserAPIKeyAuth,
    approve: bool,
    reason: str | None,
) -> ReviewSkillSubmissionResponse:
    _require_proxy_admin(caller)
    if submissions is None:
        raise _error_response(500, CommonProxyErrors.db_not_connected_error.value)
    result: Final = await submissions.review(submission_id, caller.user_id or "proxy-admin", approve, reason)
    match result:
        case SubmissionMissing():
            raise _error_response(404, "Skill submission not found")
        case SubmissionConflict(message=message):
            raise _error_response(409, message)
        case PluginPublicationError(kind="invalid_version") as publication_error:
            raise _publication_error(publication_error)
        case PluginPublicationError(message=message):
            raise _error_response(409, message)
        case SubmissionReviewed(submission=submission, plugin=plugin):
            return ReviewSkillSubmissionResponse(
                action="approved" if approve else "rejected",
                submission=_submission_response(submission),
                plugin=None if plugin is None else _plugin_response(plugin, submission.proposal),
            )
        case _:
            assert_never(result)


@router.put(
    "/claude-code/skill-submissions/{submission_id}/approve",
    tags=["Claude Code Marketplace"],
    response_model=ReviewSkillSubmissionResponse,
)
async def approve_skill_submission(
    submission_id: str,
    submissions: SkillSubmissionRepositoryDep,
    user_api_key_dict: Annotated[UserAPIKeyAuth, Depends(user_api_key_auth)],
) -> ReviewSkillSubmissionResponse:
    """Publish a pending proposal and record approval atomically. Proxy admins only; stale or reviewed proposals return 409."""
    return await _review_skill_submission(submission_id, submissions, user_api_key_dict, True, None)


@router.put(
    "/claude-code/skill-submissions/{submission_id}/reject",
    tags=["Claude Code Marketplace"],
    response_model=ReviewSkillSubmissionResponse,
)
async def reject_skill_submission(
    submission_id: str,
    submissions: SkillSubmissionRepositoryDep,
    user_api_key_dict: Annotated[UserAPIKeyAuth, Depends(user_api_key_auth)],
    request: RejectSkillSubmissionRequest | None = None,
) -> ReviewSkillSubmissionResponse:
    """Reject a pending proposal without changing the catalog. Proxy admins only; optionally record a reason."""
    return await _review_skill_submission(
        submission_id,
        submissions,
        user_api_key_dict,
        False,
        None if request is None else request.reason,
    )


@router.get(
    "/claude-code/plugins/{plugin_name}/versions",
    tags=["Claude Code Marketplace"],
    response_model=ListPluginVersionsResponse,
)
async def list_plugin_versions(
    plugin_name: str,
    uploads: PluginUploadRepositoryDep,
    user_api_key_dict: Annotated[UserAPIKeyAuth, Depends(user_api_key_auth)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ListPluginVersionsResponse:
    """Read published metadata history. External source references are not archived content."""
    if uploads is None:
        raise _error_response(500, CommonProxyErrors.db_not_connected_error.value)
    metadata: Final = await uploads.get_metadata(plugin_name)
    if metadata is None:
        raise _error_response(404, f"Plugin '{plugin_name}' not found")
    if not (await resolve_skill_visibility(user_api_key_dict)).allows(metadata):
        raise _error_response(403, f"Plugin '{plugin_name}' is not granted to this key")
    versions: Final = await uploads.versions(metadata, limit + 1, offset)
    return ListPluginVersionsResponse(
        versions=tuple(
            PluginVersionResponse(
                id=version.id,
                version=version.version,
                description=version.description,
                manifest=dict(version.manifest),
                published_at=version.published_at,
                published_by=version.published_by,
                content_kind="stored_archive" if version.archive_available else "source_reference",
                archive_path=(
                    f"/claude-code/plugins/{plugin_name}/versions/{version.id}/archive"
                    if version.archive_available
                    else None
                ),
            )
            for version in versions[:limit]
        ),
        next_offset=offset + limit if len(versions) > limit else None,
    )


@router.get(
    "/claude-code/plugins/{plugin_name}/versions/{version_id}/archive",
    tags=["Claude Code Marketplace"],
    response_class=Response,
)
async def download_plugin_version(
    plugin_name: str,
    version_id: str,
    uploads: PluginUploadRepositoryDep,
    user_api_key_dict: Annotated[UserAPIKeyAuth, Depends(user_api_key_auth)],
) -> Response:
    """Download retained uploaded bytes under the current skill's visibility policy."""
    if uploads is None:
        raise _error_response(500, CommonProxyErrors.db_not_connected_error.value)
    metadata: Final = await uploads.get_metadata(plugin_name)
    if metadata is None or not (await resolve_skill_visibility(user_api_key_dict)).allows(metadata):
        raise _error_response(404, "Skill archive not found")
    content: Final = await uploads.version_content(metadata.id, version_id, metadata)
    if content is None:
        raise _error_response(404, "This version has no retained archive")
    return Response(
        content=content,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{plugin_name}.zip"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
