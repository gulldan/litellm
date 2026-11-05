import hashlib
import io
import json
import os
import uuid
import zipfile
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Final

import psycopg
from pydantic import JsonValue

from tests.integration._support.client import JSON_OBJECT, Gateway, Scenario, list_value, object_value, string_value
from tests.integration._support.database import write_rows

PLUGINS: Final = "/claude-code/plugins"
EMPTY_HISTORY: Final[Mapping[str, JsonValue]] = {"total_downloads": 0, "downloads": [], "next_cursor": None}


@dataclass(frozen=True, slots=True)
class UploadedSkill:
    id: str
    name: str
    url: str
    digest: str
    version: str


def _delete_plugin(gateway: Gateway, name: str) -> None:
    response: Final = gateway.request("DELETE", f"{PLUGINS}/{name}")
    assert response.status_code in {200, 404}, response.text


def _upload(scenario: Scenario, *, name: str | None = None, version: str = "1.2.3") -> UploadedSkill:
    skill_name: Final = name or f"download-history-{uuid.uuid4().hex}"
    scenario.cleanups.callback(_delete_plugin, scenario.gateway, skill_name)
    content: Final = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("SKILL.md", "# Download regression\nA synthetic integration skill.\n")
    response: Final = scenario.gateway.request_multipart(
        f"{PLUGINS}/upload",
        {"metadata": json.dumps({"name": skill_name, "version": version})},
        {"file": ("skill.zip", content.getvalue(), "application/zip")},
    )
    assert response.status_code == 200, response.text
    plugin: Final = object_value(JSON_OBJECT.validate_json(response.content)["plugin"])
    source: Final = object_value(plugin["source"])
    return UploadedSkill(
        id=string_value(plugin["id"]),
        name=skill_name,
        url=string_value(source["url"]),
        digest=string_value(source["sha256"]),
        version=version,
    )


def _download(gateway: Gateway, skill: UploadedSkill, key: str | None = None) -> None:
    response: Final = gateway.client.get(skill.url, headers={"Authorization": f"Bearer {key}"} if key else {})
    assert response.status_code == 200, response.text
    assert hashlib.sha256(response.content).hexdigest() == skill.digest


def _history(
    gateway: Gateway, skill: UploadedSkill, *, limit: int = 25, cursor: str | None = None
) -> Mapping[str, JsonValue]:
    return gateway.get(
        f"{PLUGINS}/{skill.name}/downloads",
        {"limit": str(limit), **({"cursor": cursor} if cursor is not None else {})},
    )


def _events(page: Mapping[str, JsonValue]) -> tuple[Mapping[str, JsonValue], ...]:
    return tuple(object_value(event) for event in list_value(page["downloads"]))


def _ids(page: Mapping[str, JsonValue]) -> tuple[str, ...]:
    return tuple(string_value(event["id"]) for event in _events(page))


def _event_details(event: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    assert int(string_value(event["id"])) > 0, event
    assert datetime.fromisoformat(string_value(event["downloaded_at"])).tzinfo is not None, event
    return {field: value for field, value in event.items() if field not in {"id", "downloaded_at"}}


def _stored_ids(gateway: Gateway, skill: UploadedSkill) -> tuple[str, ...]:
    rows: Final = gateway.rows(
        'SELECT id::text AS id FROM "LiteLLM_ClaudeCodePluginDownload" WHERE plugin_id = %s ORDER BY id::bigint DESC',
        (skill.id,),
    )
    return tuple(string_value(row["id"]) for row in rows)


def test_downloads_count_each_request_and_keep_identity_after_actor_deletion(gateway: Gateway) -> None:
    with gateway.scenario() as skills:
        skill: Final = _upload(skills)
        assert _history(gateway, skill) == EMPTY_HISTORY
        catalog: Final = gateway.client.get("/claude-code/marketplace.json")
        assert catalog.status_code == 200, catalog.text
        assert gateway.get(f"{PLUGINS}/{skill.name}")["id"] == skill.id
        assert _history(gateway, skill) == EMPTY_HISTORY

        with gateway.scenario() as actors:
            email: Final = f"{skill.name}@example.test"
            team: Final = actors.team(team_alias=skill.name)
            user: Final = actors.user(user_role="internal_user", user_email=email)
            gateway.post("/team/member_add", {"team_id": team, "member": {"role": "user", "user_id": user}})
            key: Final = actors.key(user_id=user, team_id=team, key_alias=skill.name)
            _download(gateway, skill, key)
            assert _history(gateway, skill)["total_downloads"] == 1
            _download(gateway, skill)
            assert _history(gateway, skill)["total_downloads"] == 2
            _download(gateway, skill, key)
            recorded: Final = _history(gateway, skill)
            authenticated: Final = {
                "version": skill.version,
                "digest": skill.digest,
                "authenticated": True,
                "user_id": user,
                "user_email": email,
                "key_alias": skill.name,
                "team_id": team,
                "team_alias": skill.name,
            }
            anonymous: Final = {
                "version": skill.version,
                "digest": skill.digest,
                "authenticated": False,
                "user_id": None,
                "user_email": None,
                "key_alias": None,
                "team_id": None,
                "team_alias": None,
            }
            assert tuple(_event_details(event) for event in _events(recorded)) == (
                authenticated,
                anonymous,
                authenticated,
            )
            assert recorded["total_downloads"] == 3
            assert recorded["next_cursor"] is None
            assert len(frozenset(_ids(recorded))) == 3
            assert _stored_ids(gateway, skill) == _ids(recorded)

        assert _history(gateway, skill) == recorded


def test_denied_downloads_and_missing_archives_never_add_history(gateway: Gateway) -> None:
    with gateway.scenario() as scenario:
        skill: Final = _upload(scenario)
        key: Final = scenario.key(allowed_routes=["/claude-code/*"])
        granted: Final = scenario.key(allowed_routes=["/claude-code/*"], object_permission={"skills": [skill.name]})
        _download(gateway, skill, key)
        original: Final = _history(gateway, skill)
        assert original["total_downloads"] == 1
        private_history: Final = gateway.request("GET", f"{PLUGINS}/{skill.name}/downloads", key=key)
        assert private_history.status_code == 403, private_history.text
        anonymous_history: Final = gateway.client.get(f"{PLUGINS}/{skill.name}/downloads")
        assert anonymous_history.status_code == 401, anonymous_history.text
        wrong_digest: Final = gateway.client.get(skill.url.replace(skill.digest, "invalid-digest"))
        assert wrong_digest.status_code == 404, wrong_digest.text
        invalid_key: Final = gateway.request("GET", skill.url, key=f"sk-{uuid.uuid4().hex}")
        assert invalid_key.status_code == 401, invalid_key.text
        gateway.post(f"{PLUGINS}/{skill.name}/disable", {})
        denied: Final = (gateway.client.get(skill.url), gateway.request("GET", skill.url, key=key))
        assert tuple(response.status_code for response in denied) == (404, 404), tuple(r.text for r in denied)
        assert _history(gateway, skill) == original

        _download(gateway, skill, granted)
        admitted: Final = _history(gateway, skill)
        assert admitted["total_downloads"] == 2
        write_rows(
            'DELETE FROM "LiteLLM_ClaudeCodePluginUpload" WHERE plugin_name = %s',
            (skill.name,),
            database_url=gateway.database_url,
        )
        missing: Final = gateway.request("GET", skill.url, key=granted)
        assert missing.status_code == 404, missing.text
        assert _history(gateway, skill) == admitted
        assert _stored_ids(gateway, skill) == _ids(admitted)


def test_keyset_pages_survive_concurrent_downloads_new_insert_and_deleted_cursor(gateway: Gateway) -> None:
    with gateway.scenario() as scenario:
        skill: Final = _upload(scenario)
        other: Final = _upload(scenario)
        with ThreadPoolExecutor(max_workers=4) as pool:
            tuple(pool.map(partial(_download, gateway, skill), (None,) * 8))
        initial: Final = _history(gateway, skill)
        ordered: Final = _ids(initial)
        assert initial["total_downloads"] == len(frozenset(ordered)) == 8
        assert ordered == tuple(sorted(ordered, key=int, reverse=True))
        assert _stored_ids(gateway, skill) == ordered
        first: Final = _history(gateway, skill, limit=3)
        assert _ids(first) == ordered[:3]
        assert first["next_cursor"] == ordered[2]

        _download(gateway, skill)
        _download(gateway, other)
        write_rows(
            'DELETE FROM "LiteLLM_ClaudeCodePluginDownload" WHERE plugin_id = %s AND id IN (%s::bigint, %s::bigint)',
            (skill.id, ordered[2], ordered[4]),
            database_url=gateway.database_url,
        )
        second: Final = _history(gateway, skill, limit=3, cursor=string_value(first["next_cursor"]))
        assert _ids(second) == (ordered[3], ordered[5], ordered[6])
        assert second["total_downloads"] == 7
        assert second["next_cursor"] == ordered[6]
        last: Final = _history(gateway, skill, limit=3, cursor=string_value(second["next_cursor"]))
        assert _ids(last) == (ordered[7],)
        assert last["total_downloads"] == 7
        assert last["next_cursor"] is None
        refreshed: Final = _history(gateway, skill)
        assert len(_ids(refreshed)) == refreshed["total_downloads"] == 7
        assert int(_ids(refreshed)[0]) > int(ordered[0])
        assert _stored_ids(gateway, skill) == _ids(refreshed)
        assert _history(gateway, other, cursor=ordered[2]) == {
            "total_downloads": 1,
            "downloads": [],
            "next_cursor": None,
        }


def test_deleting_and_recreating_a_name_starts_history_for_a_new_plugin(gateway: Gateway) -> None:
    with gateway.scenario() as scenario:
        original: Final = _upload(scenario)
        _download(gateway, original)
        _download(gateway, original)
        before: Final = _history(gateway, original)
        assert before["total_downloads"] == 2
        assert _stored_ids(gateway, original) == _ids(before)
        _delete_plugin(gateway, original.name)
        missing: Final = gateway.request("GET", f"{PLUGINS}/{original.name}/downloads")
        assert missing.status_code == 404, missing.text
        assert _stored_ids(gateway, original) == ()

        recreated: Final = _upload(scenario, name=original.name, version="2.0.0")
        assert recreated.id != original.id
        assert recreated.url == original.url
        assert _history(gateway, recreated) == EMPTY_HISTORY
        _download(gateway, recreated)
        after: Final = _history(gateway, recreated)
        assert after["total_downloads"] == 1
        assert after["next_cursor"] is None
        assert len(_events(after)) == 1
        assert _events(after)[0]["version"] == recreated.version
        assert _events(after)[0]["digest"] == recreated.digest
        assert frozenset(_ids(after)).isdisjoint(_ids(before))
        assert _stored_ids(gateway, original) == ()
        assert _stored_ids(gateway, recreated) == _ids(after)


def test_repeat_migration_is_catalog_only_while_download_history_is_exclusively_locked(gateway: Gateway) -> None:
    migration: Final = (
        Path(__file__).resolve().parents[3]
        / "litellm-proxy-extras/litellm_proxy_extras/migrations/20261105094500_add_skill_download_history/migration.sql"
    ).read_bytes()
    database_url: Final = gateway.database_url or os.environ["DATABASE_URL"]
    rows: Final = (
        'SELECT to_jsonb(download) AS event FROM "LiteLLM_ClaudeCodePluginDownload" AS download '
        "WHERE plugin_id = %s ORDER BY id"
    )
    with gateway.scenario() as scenario:
        skill: Final = _upload(scenario)
        _download(gateway, skill, gateway.key)
        _download(gateway, skill)
        before: Final = gateway.rows(rows, (skill.id,))
        assert len(before) == 2
        with psycopg.connect(database_url) as locker, psycopg.connect(database_url, autocommit=True) as migrator:
            locker.execute('LOCK TABLE "LiteLLM_ClaudeCodePluginDownload" IN ACCESS EXCLUSIVE MODE')
            for _ in range(2):
                with migrator.transaction():
                    migrator.execute("SET LOCAL statement_timeout = '500ms'")
                    migrator.execute(migration)
        assert gateway.rows(rows, (skill.id,)) == before
