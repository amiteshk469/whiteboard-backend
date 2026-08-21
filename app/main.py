import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import uuid4
from weakref import WeakValueDictionary

import socketio
from fastapi import Depends, FastAPI, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .auth import create_token, current_user, passwords, public_user, token_user_id
from .db import boards, client, db, elements, ensure_indexes, find_board, users

origins = [
    origin.strip()
    for origin in os.getenv("FRONTEND_ORIGIN", "http://localhost:5173").split(",")
    if origin.strip()
]


@asynccontextmanager
async def lifespan(_: FastAPI):
    await ensure_indexes()
    yield
    await client.close()


api = FastAPI(title="Whiteboard API", lifespan=lifespan)
api.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
sio = socketio.AsyncServer(
    async_mode="asgi", cors_allowed_origins=origins, async_handlers=False
)
app = socketio.ASGIApp(sio, api)
board_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def board_lock(board_id: str) -> asyncio.Lock:
    lock = board_locks.get(board_id)
    if lock is None:
        lock = asyncio.Lock()
        board_locks[board_id] = lock
    return lock


SafeText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ElementId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
BoardId = Annotated[str, Field(min_length=1, max_length=64)]
CurrentUser = Annotated[dict, Depends(current_user)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Register(Input):
    name: Annotated[SafeText, Field(max_length=80)]
    email: Annotated[SafeText, Field(max_length=254)]
    password: Annotated[str, Field(min_length=8, max_length=128)]


class Login(Input):
    email: Annotated[SafeText, Field(max_length=254)]
    password: Annotated[str, Field(min_length=1, max_length=128)]


class BoardInput(Input):
    title: Annotated[SafeText, Field(max_length=120)]


class MemberInput(Input):
    email: Annotated[SafeText, Field(max_length=254)]


class Point(Input):
    x: float
    y: float


class Element(Input):
    id: ElementId
    kind: Literal["note", "text", "rectangle", "ellipse", "line", "pen"]
    x: float
    y: float
    width: Annotated[float, Field(ge=-1_000_000, le=1_000_000)]
    height: Annotated[float, Field(ge=-1_000_000, le=1_000_000)]
    text: Annotated[str, Field(max_length=10_000)] = ""
    color: Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}$")] = "#111827"
    points: list[Point] = Field(default_factory=list, max_length=5_000)
    version: Annotated[int, Field(ge=0)] = 0


class Join(Input):
    boardId: BoardId


class Upsert(Input):
    boardId: BoardId
    element: Element


class Delete(Input):
    boardId: BoardId
    id: ElementId
    version: Annotated[int, Field(ge=1)]


class Cursor(Input):
    boardId: BoardId
    x: float
    y: float


def now() -> datetime:
    return datetime.now(UTC)


def normalize_email(email: str) -> str:
    email = email.strip().lower()
    if email.count("@") != 1 or "." not in email.rsplit("@", 1)[1]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid email")
    return email


def public_board(board: dict) -> dict:
    return {
        "id": board["_id"],
        "title": board["title"],
        "ownerId": board["ownerId"],
        "memberIds": board["memberIds"],
        "createdAt": board["createdAt"],
        "updatedAt": board["updatedAt"],
    }


def public_element(element: dict) -> dict:
    return {key: element[key] for key in Element.model_fields}


async def require_board(board_id: str, user_id: str, *, owner: bool = False):
    board = await find_board(board_id, user_id, owner=owner)
    if not board:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Board not found")
    return board


@api.get("/health")
async def health():
    await db.command("ping")
    return {"ok": True}


@api.post("/api/auth/register", status_code=status.HTTP_201_CREATED)
async def register(data: Register):
    user = {
        "_id": str(uuid4()),
        "name": data.name,
        "email": normalize_email(data.email),
        "passwordHash": await asyncio.to_thread(passwords.hash, data.password),
        "createdAt": now(),
    }
    try:
        await users.insert_one(user)
    except DuplicateKeyError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Email already registered"
        ) from None
    return {"token": create_token(user["_id"]), "user": public_user(user)}


@api.post("/api/auth/login")
async def login(data: Login):
    user = await users.find_one({"email": normalize_email(data.email)})
    valid = user and await asyncio.to_thread(
        passwords.verify, data.password, user["passwordHash"]
    )
    if not valid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    return {"token": create_token(user["_id"]), "user": public_user(user)}


@api.get("/api/auth/me")
async def me(user: CurrentUser):
    return public_user(user)


@api.post("/api/boards", status_code=status.HTTP_201_CREATED)
async def create_board(data: BoardInput, user: CurrentUser):
    timestamp = now()
    board = {
        "_id": str(uuid4()),
        "title": data.title,
        "ownerId": user["_id"],
        "memberIds": [],
        "createdAt": timestamp,
        "updatedAt": timestamp,
    }
    await boards.insert_one(board)
    return public_board(board)


@api.get("/api/boards")
async def list_boards(user: CurrentUser):
    query = {"$or": [{"ownerId": user["_id"]}, {"memberIds": user["_id"]}]}
    return [
        public_board(board) async for board in boards.find(query).sort("updatedAt", -1)
    ]


@api.get("/api/boards/{board_id}")
async def get_board(board_id: str, user: CurrentUser):
    return public_board(await require_board(board_id, user["_id"]))


@api.patch("/api/boards/{board_id}")
async def rename_board(board_id: str, data: BoardInput, user: CurrentUser):
    await require_board(board_id, user["_id"], owner=True)
    board = await boards.find_one_and_update(
        {"_id": board_id},
        {"$set": {"title": data.title, "updatedAt": now()}},
        return_document=ReturnDocument.AFTER,
    )
    return public_board(board)


@api.delete("/api/boards/{board_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_board(board_id: str, user: CurrentUser):
    async with board_lock(board_id):
        await require_board(board_id, user["_id"], owner=True)
        await boards.delete_one({"_id": board_id})
        await elements.delete_many({"boardId": board_id})
        room = f"board:{board_id}"
        await sio.emit("board:deleted", {"boardId": board_id}, room=room)
        await sio.close_room(room)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@api.post("/api/boards/{board_id}/members")
async def add_member(board_id: str, data: MemberInput, user: CurrentUser):
    await require_board(board_id, user["_id"], owner=True)
    member = await users.find_one({"email": normalize_email(data.email)})
    if not member:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    board = await boards.find_one_and_update(
        {"_id": board_id},
        {
            "$addToSet": {"memberIds": member["_id"]},
            "$set": {"updatedAt": now()},
        },
        return_document=ReturnDocument.AFTER,
    )
    return public_board(board)


@api.get("/api/boards/{board_id}/elements")
async def list_elements(board_id: str, user: CurrentUser):
    await require_board(board_id, user["_id"])
    return [
        public_element(element)
        async for element in elements.find({"boardId": board_id}).sort("id", 1)
    ]


def invalid_payload():
    return {"ok": False, "error": "invalid payload"}


async def socket_access(sid: str, board_id: str):
    try:
        session = await sio.get_session(sid)
    except KeyError:
        return None, None
    return session, await find_board(board_id, session["userId"])


@sio.event
async def connect(sid, _environ, auth):
    user_id = token_user_id(auth.get("token", "")) if isinstance(auth, dict) else None
    user = await users.find_one({"_id": user_id}) if user_id else None
    if not user:
        raise socketio.exceptions.ConnectionRefusedError("unauthorized")
    await sio.save_session(
        sid,
        {"userId": user["_id"], "name": user["name"], "boards": set()},
    )


@sio.on("board:join")
async def board_join(sid, raw):
    try:
        data = Join.model_validate(raw)
    except ValidationError:
        return invalid_payload()
    async with board_lock(data.boardId):
        session, board = await socket_access(sid, data.boardId)
        if not board:
            return {"ok": False, "error": "board not found"}
        await sio.enter_room(sid, f"board:{data.boardId}")
        session["boards"].add(data.boardId)
        await sio.save_session(sid, session)
        snapshot = [
            public_element(element)
            async for element in elements.find({"boardId": data.boardId}).sort("id", 1)
        ]
        await sio.emit(
            "board:snapshot",
            {"boardId": data.boardId, "elements": snapshot},
            to=sid,
        )
    return {"ok": True, "boardId": data.boardId}


@sio.on("element:preview")
async def element_preview(sid, raw):
    try:
        data = Upsert.model_validate(raw)
        session = await sio.get_session(sid)
    except (ValidationError, KeyError):
        return invalid_payload()
    if data.boardId not in session["boards"]:
        return {"ok": False, "error": "join board first"}
    await sio.emit(
        "element:preview",
        {
            "boardId": data.boardId,
            "userId": session["userId"],
            "element": data.element.model_dump(),
        },
        room=f"board:{data.boardId}",
        skip_sid=sid,
    )
    return {"ok": True}


@sio.on("element:upsert")
async def element_upsert(sid, raw):
    try:
        data = Upsert.model_validate(raw)
    except ValidationError:
        return invalid_payload()
    async with board_lock(data.boardId):
        session, board = await socket_access(sid, data.boardId)
        if not board:
            return {"ok": False, "error": "board not found"}

        incoming = data.element.model_dump()
        version = incoming["version"]
        document = {
            **incoming,
            "boardId": data.boardId,
            "version": version + 1,
            "updatedBy": session["userId"],
            "updatedAt": now(),
        }
        if version == 0:
            try:
                await elements.insert_one(document)
                saved = document
            except DuplicateKeyError:
                saved = None
        else:
            saved = await elements.find_one_and_update(
                {"boardId": data.boardId, "id": incoming["id"], "version": version},
                {"$set": document},
                return_document=ReturnDocument.AFTER,
            )
        if not saved:
            current = await elements.find_one(
                {"boardId": data.boardId, "id": incoming["id"]}
            )
            response = {"ok": False, "error": "version conflict"}
            if current:
                authoritative = public_element(current)
                response["element"] = authoritative
                await sio.emit(
                    "element:upsert",
                    {"boardId": data.boardId, "element": authoritative},
                    room=f"board:{data.boardId}",
                    skip_sid=sid,
                )
            else:
                response["deleted"] = True
                await sio.emit(
                    "element:delete",
                    {"boardId": data.boardId, "id": incoming["id"]},
                    room=f"board:{data.boardId}",
                    skip_sid=sid,
                )
            return response

        await boards.update_one({"_id": data.boardId}, {"$set": {"updatedAt": now()}})
        payload = {"boardId": data.boardId, "element": public_element(saved)}
        await sio.emit(
            "element:upsert", payload, room=f"board:{data.boardId}", skip_sid=sid
        )
    return {"ok": True, "element": payload["element"]}


@sio.on("element:delete")
async def element_delete(sid, raw):
    try:
        data = Delete.model_validate(raw)
    except ValidationError:
        return invalid_payload()
    async with board_lock(data.boardId):
        _session, board = await socket_access(sid, data.boardId)
        if not board:
            return {"ok": False, "error": "board not found"}
        deleted = await elements.delete_one(
            {"boardId": data.boardId, "id": data.id, "version": data.version}
        )
        if not deleted.deleted_count:
            current = await elements.find_one({"boardId": data.boardId, "id": data.id})
            if current:
                return {
                    "ok": False,
                    "error": "version conflict",
                    "element": public_element(current),
                }
            return {"ok": True, "id": data.id}
        await boards.update_one({"_id": data.boardId}, {"$set": {"updatedAt": now()}})
        payload = {"boardId": data.boardId, "id": data.id}
        await sio.emit(
            "element:delete", payload, room=f"board:{data.boardId}", skip_sid=sid
        )
    return {"ok": True, "id": data.id}


@sio.on("cursor:move")
async def cursor_move(sid, raw):
    try:
        data = Cursor.model_validate(raw)
        session = await sio.get_session(sid)
    except (ValidationError, KeyError):
        return invalid_payload()
    if data.boardId not in session["boards"]:
        return {"ok": False, "error": "join board first"}
    payload = {
        "boardId": data.boardId,
        "userId": session["userId"],
        "name": session["name"],
        "x": data.x,
        "y": data.y,
    }
    await sio.emit("cursor:move", payload, room=f"board:{data.boardId}", skip_sid=sid)
    return {"ok": True}


@sio.event
async def disconnect(sid, _reason):
    try:
        session = await sio.get_session(sid)
    except KeyError:
        return
    for board_id in session["boards"]:
        await sio.emit(
            "cursor:leave",
            {"boardId": board_id, "userId": session["userId"]},
            room=f"board:{board_id}",
            skip_sid=sid,
        )
