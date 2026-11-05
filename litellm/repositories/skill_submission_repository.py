from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Final, Protocol, cast  # noqa: TID251  # Prisma's generated delegates are dynamic

from pydantic import BaseModel, ConfigDict, TypeAdapter

from litellm.models.plugin_upload import (
    PluginPublication,
    PluginPublicationBase,
    PluginPublicationError,
    PublishedPlugin,
)
from litellm.models.skill_submission import (
    SkillSubmission,
    SubmissionConflict,
    SubmissionMissing,
    SubmissionOperation,
    SubmissionReviewed,
    SubmissionReviewResult,
    SubmissionStatus,
)
from litellm.repositories.plugin_publication import PluginPublicationTransaction, publish_plugin
from litellm.repositories.prisma_protocols import TableActions
from litellm.types.proxy.claude_code_endpoints import RegisterPluginRequest

if TYPE_CHECKING:
    from prisma import models as prisma_models

    from litellm.proxy.utils import PrismaClient


class _StoredProposal(BaseModel):
    model_config = ConfigDict(frozen=True)

    manifest: Mapping[str, object]
    version: str | None


class _SubmissionRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    plugin_name: str
    operation: SubmissionOperation
    status: SubmissionStatus
    proposal_json: str
    has_archive: bool
    submitted_by: str
    created_at: datetime
    base_plugin_id: str | None
    base_updated_at: datetime | None
    reviewed_by: str | None
    reviewed_at: datetime | None
    rejection_reason: str | None

    def submission(self) -> SkillSubmission:
        stored: Final = _StoredProposal.model_validate_json(self.proposal_json)
        return SkillSubmission(
            id=self.id,
            plugin_name=self.plugin_name,
            operation=self.operation,
            status=self.status,
            proposal=RegisterPluginRequest.model_validate({**stored.manifest, "version": stored.version}),
            has_archive=self.has_archive,
            submitted_by=self.submitted_by,
            created_at=self.created_at,
            base_plugin_id=self.base_plugin_id,
            base_updated_at=self.base_updated_at,
            reviewed_by=self.reviewed_by,
            reviewed_at=self.reviewed_at,
            rejection_reason=self.rejection_reason,
        )


_ROWS: Final = TypeAdapter(tuple[_SubmissionRow, ...])
_SELECT: Final = """
SELECT id, plugin_name, operation, status, proposal_json,
       archive_content IS NOT NULL AS has_archive, submitted_by, created_at,
       base_plugin_id, base_updated_at, reviewed_by, reviewed_at, rejection_reason
FROM "LiteLLM_SkillSubmission"
"""


class SubmissionTransaction(PluginPublicationTransaction, Protocol):
    @property
    def litellm_skillsubmission(self) -> TableActions["prisma_models.LiteLLM_SkillSubmission"]: ...


class SubmissionDatabase(SubmissionTransaction, Protocol):
    def tx(self) -> AbstractAsyncContextManager[SubmissionTransaction]: ...


def _submission(row: "prisma_models.LiteLLM_SkillSubmission") -> SkillSubmission:
    return _SubmissionRow.model_validate(
        {**row.model_dump(), "has_archive": row.archive_content is not None}
    ).submission()


class SkillSubmissionRepository:
    def __init__(self, prisma_client: "PrismaClient") -> None:
        self._database = cast(  # cast-ok: Prisma dynamically exposes generated delegates and transactions
            SubmissionDatabase, prisma_client.writer_db
        )

    async def submit(
        self, upload: PluginPublication, submitted_by: str, base: PluginPublicationBase | None
    ) -> SkillSubmission:
        from prisma import Base64

        row: Final = await self._database.litellm_skillsubmission.create(
            data={
                "plugin_name": upload.name,
                "operation": "create" if base is None else "update",
                "proposal_json": _StoredProposal(
                    manifest=dict(upload.manifest), version=upload.version
                ).model_dump_json(),
                "archive_content": None if upload.content is None else Base64.encode(upload.content),
                "submitted_by": submitted_by,
                "base_plugin_id": None if base is None else base.id,
                "base_updated_at": None if base is None else base.updated_at,
            }
        )
        return _submission(row)

    async def list(self, submitted_by: str | None, status: SubmissionStatus | None) -> tuple[SkillSubmission, ...]:
        rows: Final = _ROWS.validate_python(
            await self._database.query_raw(
                _SELECT
                + """WHERE ($1::text IS NULL OR submitted_by = $1)
                     AND ($2::text IS NULL OR status = $2) ORDER BY created_at DESC, id DESC""",
                submitted_by,
                status,
            )
        )
        return tuple(row.submission() for row in rows)

    async def get(self, submission_id: str, submitted_by: str | None) -> SkillSubmission | None:
        rows: Final = _ROWS.validate_python(
            await self._database.query_raw(
                _SELECT + "WHERE id = $1 AND ($2::text IS NULL OR submitted_by = $2)",
                submission_id,
                submitted_by,
            )
        )
        return None if not rows else rows[0].submission()

    async def get_archive(self, submission_id: str, submitted_by: str | None) -> bytes | None:
        row: Final = await self._database.litellm_skillsubmission.find_first(
            where={"id": submission_id, **({"submitted_by": submitted_by} if submitted_by is not None else {})}
        )
        return None if row is None or row.archive_content is None else row.archive_content.decode()

    async def review(
        self, submission_id: str, reviewer: str, approve: bool, reason: str | None
    ) -> SubmissionReviewResult:
        from prisma.errors import UniqueViolationError

        try:
            async with self._database.tx() as transaction:
                await transaction.query_raw(
                    'SELECT id FROM "LiteLLM_SkillSubmission" WHERE id = $1 FOR UPDATE', submission_id
                )
                row: Final = await transaction.litellm_skillsubmission.find_unique(where={"id": submission_id})
                if row is None:
                    return SubmissionMissing()
                submission: Final = _submission(row)
                if submission.status != "pending_review":
                    return SubmissionConflict("This submission has already been reviewed.")
                publication: Final = await _publish_submission(transaction, row) if approve else None
                if isinstance(publication, PluginPublicationError):
                    return publication
                reviewed_at: Final = datetime.now(timezone.utc)
                status: Final[SubmissionStatus] = "approved" if approve else "rejected"
                await transaction.litellm_skillsubmission.update(
                    where={"id": submission_id},
                    data={
                        "status": status,
                        "reviewed_by": reviewer,
                        "reviewed_at": reviewed_at,
                        "rejection_reason": None if approve else reason,
                    },
                )
                return SubmissionReviewed(
                    submission=replace(
                        submission,
                        status=status,
                        reviewed_by=reviewer,
                        reviewed_at=reviewed_at,
                        rejection_reason=None if approve else reason,
                    ),
                    plugin=publication,
                )
        except UniqueViolationError:
            return SubmissionConflict("A skill with this name is already published. Submit a new update proposal.")


async def _publish_submission(
    transaction: PluginPublicationTransaction, row: "prisma_models.LiteLLM_SkillSubmission"
) -> PublishedPlugin | PluginPublicationError:
    stored: Final = _StoredProposal.model_validate_json(row.proposal_json)
    proposal: Final = RegisterPluginRequest.model_validate({**stored.manifest, "version": stored.version})
    base: Final = (
        None
        if row.operation == "create"
        else PluginPublicationBase(id=row.base_plugin_id or "", updated_at=row.base_updated_at)
    )
    return await publish_plugin(
        transaction,
        PluginPublication(
            name=row.plugin_name,
            version=stored.version,
            description=proposal.description,
            manifest=stored.manifest,
            content=None if row.archive_content is None else row.archive_content.decode(),
            created_by=row.submitted_by,
        ),
        base,
    )
