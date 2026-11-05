import io
import json
import os
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from typing import Final

import psycopg
from psycopg import sql

from tests.integration._support.client import Gateway, list_value, object_value, string_value
from tests.integration._support.database import read_rows


def _archive() -> bytes:
    buffer: Final = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("SKILL.md", "# Published instructions\nRetain these exact bytes.\n")
    return buffer.getvalue()


def _delete(gateway: Gateway, name: str) -> None:
    response: Final = gateway.request("DELETE", f"/claude-code/plugins/{name}")
    assert response.status_code == 200, response.text


def test_snapshot_failure_rolls_back_catalog_archive_and_history_then_source_replacement_preserves_bytes(
    gateway: Gateway,
) -> None:
    name: Final = f"integration-version-{uuid.uuid4().hex}"
    path: Final = f"/claude-code/plugins/{name}"
    content: Final = _archive()
    with ExitStack() as cleanup:
        created: Final = gateway.request_multipart(
            "/claude-code/plugins/upload",
            {"metadata": json.dumps({"name": name})},
            {"file": ("skill.zip", content, "application/zip")},
        )
        assert created.status_code == 200, created.text
        cleanup.callback(_delete, gateway, name)
        before: Final = gateway.get(path)
        history: Final = gateway.get(f"{path}/versions")
        original: Final = object_value(list_value(history["versions"])[0])
        archive_path: Final = string_value(original["archive_path"])
        original_response: Final = gateway.request("GET", archive_path)
        assert original_response.status_code == 200, original_response.text
        published_content: Final = original_response.content
        with zipfile.ZipFile(io.BytesIO(published_content)) as published, zipfile.ZipFile(io.BytesIO(content)) as uploaded:
            assert published.read(f"skills/{name}/SKILL.md") == uploaded.read("SKILL.md")
        witness: Final = f"publication_{uuid.uuid4().hex}"
        trigger: Final = f"{witness}_trigger"
        function: Final = f"{witness}_function"
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as connection, ExitStack() as failure:
            connection.execute(sql.SQL("CREATE SEQUENCE {}").format(sql.Identifier(witness)))
            failure.callback(connection.execute, sql.SQL("DROP SEQUENCE {}").format(sql.Identifier(witness)))
            connection.execute(sql.SQL("GRANT USAGE ON SEQUENCE {} TO PUBLIC").format(sql.Identifier(witness)))
            connection.execute(
                sql.SQL(
                    "CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                    "IF EXISTS (SELECT 1 FROM \"LiteLLM_ClaudeCodePluginTable\" "
                    "WHERE id = NEW.plugin_id AND name = {}) THEN PERFORM nextval({}); "
                    "RAISE EXCEPTION 'publication snapshot failure'; END IF; RETURN NEW; END $$"
                ).format(sql.Identifier(function), sql.Literal(name), sql.Literal(witness))
            )
            failure.callback(connection.execute, sql.SQL("DROP FUNCTION {}()").format(sql.Identifier(function)))
            connection.execute(
                sql.SQL('CREATE TRIGGER {} BEFORE INSERT ON "LiteLLM_ClaudeCodePluginVersion" '
                        "FOR EACH ROW EXECUTE FUNCTION {}()").format(sql.Identifier(trigger), sql.Identifier(function))
            )
            failure.callback(
                connection.execute,
                sql.SQL('DROP TRIGGER {} ON "LiteLLM_ClaudeCodePluginVersion"').format(sql.Identifier(trigger)),
            )
            rejected: Final = gateway.request(
                "PUT", path, {"source": {"source": "github", "repo": "example/changed"}, "description": "must roll back"},
            )
            assert rejected.status_code == 500, rejected.text
            assert connection.execute(
                sql.SQL("SELECT is_called FROM {}").format(sql.Identifier(witness))
            ).fetchone() == (True,)
            assert gateway.get(path) == before
            assert gateway.get(f"{path}/versions") == history
            current_archive: Final = gateway.request("GET", string_value(object_value(before["source"])["url"]))
            assert current_archive.status_code == 200 and current_archive.content == published_content
        updated: Final = gateway.request(
            "PUT", path, {"source": {"source": "github", "repo": "example/changed"}},
        )
        assert updated.status_code == 200, updated.text
        after: Final = gateway.get(f"{path}/versions")
        versions: Final = tuple(object_value(value) for value in list_value(after["versions"]))
        assert [version["version"] for version in versions] == ["1.0.1", "1.0.0"]
        assert versions[0]["content_kind"] == "source_reference"
        assert versions[1]["content_kind"] == "stored_archive"
        retained: Final = gateway.request("GET", archive_path)
        assert retained.status_code == 200 and retained.content == published_content
        assert read_rows(
            'SELECT plugin_name FROM "LiteLLM_ClaudeCodePluginUpload" WHERE plugin_name=%s', (name,)
        ) == []


def test_competing_publications_allow_one_original_base_and_preserve_both_releases(gateway: Gateway) -> None:
    name: Final = f"integration-version-{uuid.uuid4().hex}"
    path: Final = f"/claude-code/plugins/{name}"
    with ExitStack() as cleanup:
        gateway.post("/claude-code/plugins", {"name": name, "source": {"source": "github", "repo": "example/original"}})
        cleanup.callback(_delete, gateway, name)
        before: Final = gateway.get(path)

        def publish(index: int) -> int:
            return gateway.request(
                "PUT", path,
                {
                    "source": {"source": "github", "repo": f"example/edit-{index}"},
                    "expected_updated_at": before["updated_at"],
                },
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses: Final = tuple(pool.map(publish, range(2)))
        assert sorted(statuses) == [200, 409]
        history: Final = gateway.get(f"{path}/versions")
        versions: Final = tuple(object_value(value) for value in list_value(history["versions"]))
        assert [version["version"] for version in versions] == ["1.0.1", "1.0.0"]
        assert gateway.get(path)["version"] == versions[0]["version"]
