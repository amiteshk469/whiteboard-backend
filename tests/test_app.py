import asyncio
import socket

import httpx
import pytest
import pytest_asyncio
import socketio
import uvicorn

from app.db import db
from app.main import app


@pytest_asyncio.fixture
async def server():
    if db.name != "whiteboard_test":
        pytest.fail("Refusing to clear a non-test database")
    try:
        await db.command("ping")
    except Exception as error:
        pytest.fail(f"MongoDB is unavailable: {error}")
    await db.client.drop_database(db.name)

    with socket.socket() as free_socket:
        free_socket.bind(("127.0.0.1", 0))
        port = free_socket.getsockname()[1]
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    task = asyncio.create_task(uvicorn_server.serve())
    for _ in range(50):
        if uvicorn_server.started:
            break
        await asyncio.sleep(0.1)
    assert uvicorn_server.started
    yield f"http://127.0.0.1:{port}"
    uvicorn_server.should_exit = True
    await task


@pytest.mark.asyncio
async def test_api_realtime_and_persistence(server):
    async with httpx.AsyncClient(base_url=server) as http:
        assert (await http.get("/health")).json() == {"ok": True}
        owner = await http.post(
            "/api/auth/register",
            json={
                "name": "Owner",
                "email": "owner@example.com",
                "password": "password1",
            },
        )
        member = await http.post(
            "/api/auth/register",
            json={
                "name": "Member",
                "email": "member@example.com",
                "password": "password2",
            },
        )
        assert owner.status_code == member.status_code == 201
        owner_token = owner.json()["token"]
        member_token = member.json()["token"]
        member_id = member.json()["user"]["id"]
        owner_headers = {"Authorization": f"Bearer {owner_token}"}
        member_headers = {"Authorization": f"Bearer {member_token}"}

        assert (await http.get("/api/auth/me", headers=owner_headers)).json()[
            "name"
        ] == "Owner"
        login = await http.post(
            "/api/auth/login",
            json={"email": "owner@example.com", "password": "password1"},
        )
        assert login.status_code == 200

        created = await http.post(
            "/api/boards", json={"title": "Ideas"}, headers=owner_headers
        )
        assert created.status_code == 201
        board_id = created.json()["id"]
        added = await http.post(
            f"/api/boards/{board_id}/members",
            json={"email": "member@example.com"},
            headers=owner_headers,
        )
        assert member_id in added.json()["memberIds"]
        assert len((await http.get("/api/boards", headers=member_headers)).json()) == 1
        renamed = await http.patch(
            f"/api/boards/{board_id}",
            json={"title": "Planning"},
            headers=owner_headers,
        )
        assert renamed.json()["title"] == "Planning"
        assert (
            await http.get(f"/api/boards/{board_id}", headers=member_headers)
        ).json()["title"] == "Planning"
        forbidden = await http.patch(
            f"/api/boards/{board_id}",
            json={"title": "Nope"},
            headers=member_headers,
        )
        assert forbidden.status_code == 404

        first = socketio.AsyncClient()
        second = socketio.AsyncClient()
        rejected = socketio.AsyncClient()
        received = asyncio.Event()
        first_snapshot_received = asyncio.Event()
        second_snapshot_received = asyncio.Event()
        preview_received = asyncio.Event()
        conflict_received = asyncio.Event()
        remote_element = None
        preview_element = None
        conflict_element = None
        first_snapshot = None
        second_snapshot = None

        @first.on("board:snapshot")
        async def receive_first_snapshot(payload):
            nonlocal first_snapshot
            first_snapshot = payload
            first_snapshot_received.set()

        @second.on("board:snapshot")
        async def receive_second_snapshot(payload):
            nonlocal second_snapshot
            second_snapshot = payload
            second_snapshot_received.set()

        @second.on("element:upsert")
        async def receive_element(payload):
            nonlocal remote_element
            remote_element = payload["element"]
            received.set()

        @second.on("element:preview")
        async def receive_preview(payload):
            nonlocal preview_element
            preview_element = payload["element"]
            preview_received.set()

        @first.on("element:upsert")
        async def receive_conflict(payload):
            nonlocal conflict_element
            conflict_element = payload["element"]
            conflict_received.set()

        with pytest.raises(socketio.exceptions.ConnectionError):
            await rejected.connect(server, auth={"token": "invalid"})
        await first.connect(server, auth={"token": owner_token})
        await second.connect(server, auth={"token": member_token})
        assert (await first.call("board:join", {"boardId": board_id}))["ok"]
        assert (await second.call("board:join", {"boardId": board_id}))["ok"]
        await asyncio.wait_for(first_snapshot_received.wait(), timeout=2)
        await asyncio.wait_for(second_snapshot_received.wait(), timeout=2)
        assert first_snapshot["elements"] == second_snapshot["elements"] == []

        note = {
            "id": "note_1",
            "kind": "note",
            "x": 20,
            "y": 30,
            "width": 220,
            "height": 160,
            "text": "Persist me",
            "color": "#FDE68A",
            "points": [],
            "version": 0,
        }
        assert (
            await first.call("element:preview", {"boardId": board_id, "element": note})
        )["ok"]
        await asyncio.wait_for(preview_received.wait(), timeout=2)
        assert preview_element == note
        assert (
            await http.get(f"/api/boards/{board_id}/elements", headers=owner_headers)
        ).json() == []
        saved = await first.call(
            "element:upsert", {"boardId": board_id, "element": note}
        )
        assert saved["ok"] and saved["element"]["version"] == 1
        await asyncio.wait_for(received.wait(), timeout=2)
        assert remote_element == saved["element"]

        stale = await second.call(
            "element:upsert", {"boardId": board_id, "element": note}
        )
        assert not stale["ok"] and stale["element"]["version"] == 1
        await asyncio.wait_for(conflict_received.wait(), timeout=2)
        assert conflict_element == saved["element"]
        await first.disconnect()
        changed = {**saved["element"], "text": "Updated"}
        updated = await second.call(
            "element:upsert", {"boardId": board_id, "element": changed}
        )
        assert updated["ok"] and updated["element"]["version"] == 2
        stored = await http.get(
            f"/api/boards/{board_id}/elements", headers=member_headers
        )
        assert stored.json() == [updated["element"]]

        reconnected = socketio.AsyncClient()
        reconnect_snapshot_received = asyncio.Event()
        reconnect_snapshot = None

        @reconnected.on("board:snapshot")
        async def receive_reconnect_snapshot(payload):
            nonlocal reconnect_snapshot
            reconnect_snapshot = payload
            reconnect_snapshot_received.set()

        await reconnected.connect(server, auth={"token": owner_token})
        assert (await reconnected.call("board:join", {"boardId": board_id}))["ok"]
        await asyncio.wait_for(reconnect_snapshot_received.wait(), timeout=2)
        assert reconnect_snapshot["elements"] == [updated["element"]]

        cursor_received = asyncio.Event()
        cursor_payload = None

        @reconnected.on("cursor:move")
        async def receive_cursor(payload):
            nonlocal cursor_payload
            cursor_payload = payload
            cursor_received.set()

        assert (
            await second.call("cursor:move", {"boardId": board_id, "x": 400, "y": 250})
        )["ok"]
        await asyncio.wait_for(cursor_received.wait(), timeout=2)
        assert cursor_payload["userId"] == member_id

        stale_delete = await reconnected.call(
            "element:delete", {"boardId": board_id, "id": "note_1", "version": 1}
        )
        assert not stale_delete["ok"]
        assert stale_delete["element"] == updated["element"]

        deleted = await second.call(
            "element:delete", {"boardId": board_id, "id": "note_1", "version": 2}
        )
        assert deleted == {"ok": True, "id": "note_1"}
        assert (
            await http.get(f"/api/boards/{board_id}/elements", headers=owner_headers)
        ).json() == []
        assert await reconnected.call(
            "element:delete", {"boardId": board_id, "id": "note_1", "version": 2}
        ) == {"ok": True, "id": "note_1"}

        conflict_delete_received = asyncio.Event()

        @reconnected.on("element:delete")
        async def receive_conflict_delete(payload):
            if payload["id"] == "note_1":
                conflict_delete_received.set()

        missing = await second.call(
            "element:upsert", {"boardId": board_id, "element": updated["element"]}
        )
        assert missing == {"ok": False, "error": "version conflict", "deleted": True}
        await asyncio.wait_for(conflict_delete_received.wait(), timeout=2)

        left = asyncio.Event()

        @second.on("cursor:leave")
        async def receive_leave(payload):
            if payload["userId"] == owner.json()["user"]["id"]:
                left.set()

        await reconnected.disconnect()
        await asyncio.wait_for(left.wait(), timeout=2)

        board_deleted = asyncio.Event()

        @second.on("board:deleted")
        async def receive_board_deleted(payload):
            if payload["boardId"] == board_id:
                board_deleted.set()

        removed = await http.delete(f"/api/boards/{board_id}", headers=owner_headers)
        assert removed.status_code == 204
        await asyncio.wait_for(board_deleted.wait(), timeout=2)
        assert (
            await http.get(f"/api/boards/{board_id}", headers=member_headers)
        ).status_code == 404
        await second.disconnect()
