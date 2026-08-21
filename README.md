# Whiteboard backend

Small FastAPI + Socket.IO API for a persistent collaborative board. MongoDB is
the source of truth; Socket.IO only carries live changes.

## Run it

The shortest local setup uses Docker:

```bash
cp .env.example .env
python -c 'import secrets; print(secrets.token_urlsafe(32))'
# Paste that value after JWT_SECRET= in .env.
docker compose up --build
```

The API is then at `http://localhost:8000`, its interactive documentation is at
`http://localhost:8000/docs`, and MongoDB is at `localhost:27017`.

To run Python directly, start MongoDB and use:

```bash
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
uvicorn app.main:app --reload --env-file .env
```

The deliberately invalid example secret makes copied production configuration
fail closed. `JWT_SECRET` must contain at least 32 random characters.

## How persistence works

MongoDB has three collections:

- `users`: identity, email, and Argon2 password hash.
- `boards`: title, owner, members, and timestamps.
- `elements`: one document per board object, uniquely indexed by `(boardId, id)`.

The client gives each element a time-prefixed random ID, which keeps new objects
on top while remaining deterministic across browsers. It sends version `0`
when creating an element. MongoDB stores version
`1`. Each later update must send the stored version; the server atomically
matches it and increments it. A stale update receives the current element
instead of overwriting it. Only a successful database write is broadcast.

Cursor movement and throttled drag/draw previews are intentionally not stored.
The final `element:upsert` is the durable write. Logging out disconnects that
user but never deletes a board. Every join and reconnect receives an
authoritative MongoDB snapshot, so edits made during a disconnection are
recovered.

## HTTP API

Except for registration, login, and health, send
`Authorization: Bearer <token>`.

| Method | Path | Body | Result |
| --- | --- | --- | --- |
| `GET` | `/health` | — | MongoDB-backed health check |
| `POST` | `/api/auth/register` | `{name,email,password}` | `{token,user}` |
| `POST` | `/api/auth/login` | `{email,password}` | `{token,user}` |
| `GET` | `/api/auth/me` | — | current user |
| `POST` | `/api/boards` | `{title}` | new board |
| `GET` | `/api/boards` | — | accessible boards |
| `GET` | `/api/boards/{id}` | — | one accessible board |
| `PATCH` | `/api/boards/{id}` | `{title}` | renamed board (owner only) |
| `DELETE` | `/api/boards/{id}` | — | `204` (owner only) |
| `POST` | `/api/boards/{id}/members` | `{email}` | updated board (owner only) |
| `GET` | `/api/boards/{id}/elements` | — | saved elements |

A user is `{id,name,email}`. A board is
`{id,title,ownerId,memberIds,createdAt,updatedAt}`.

## Socket.IO API

Connect with `auth: {token}`. Every handler acknowledges with `{ok:true,...}` or
`{ok:false,error,...}`.

| Event | Client payload | Server broadcast |
| --- | --- | --- |
| `board:join` | `{boardId}` | `board:snapshot {boardId,elements}` to that client |
| `element:preview` | `{boardId,element}` | `{boardId,userId,element}` without a database write |
| `element:upsert` | `{boardId,element}` | `{boardId,element}` |
| `element:delete` | `{boardId,id,version}` | `{boardId,id}` |
| `cursor:move` | `{boardId,x,y}` | `{boardId,userId,name,x,y}` |

The server also emits `cursor:leave {boardId,userId}` after a disconnect and
`board:deleted {boardId}` before closing a deleted board's room.

An element is:

```json
{
  "id": "note_1",
  "kind": "note",
  "x": 20,
  "y": 30,
  "width": 220,
  "height": 160,
  "text": "An idea",
  "color": "#FDE68A",
  "points": [{"x": 0, "y": 0}],
  "version": 0
}
```

`board:join` checks database access. Element writes check access again, so a
removed user cannot keep editing through an old socket. Cursor and preview
events only work after joining the board room. Elements are sorted by their
client-generated IDs so every browser uses the same stacking order. A small,
weakly held per-board lock orders snapshots, writes, and deletion inside the
deliberately single-worker process without retaining locks for inactive boards.

## Test it

With MongoDB running:

```bash
pip install -e '.[dev]'
ruff check .
ruff format --check .
pytest
```

The test starts the real ASGI app, registers two users, shares a board, connects
two Socket.IO clients, verifies non-persistent previews, durable broadcasts,
conflicts, reconnect snapshots, cursor departure, idempotent deletion, and
permissions. It fails if MongoDB is unavailable and refuses to clear any
database except `whiteboard_test`.

## Deploy it

`render.yaml` defines one Docker web service and a `/health` check. Connect this
repository to Render, set `MONGODB_URI` to a MongoDB Atlas connection string and
`FRONTEND_ORIGIN` to the deployed frontend URL, then choose **After CI Checks
Pass** for Render Auto-Deploy. Render generates `JWT_SECRET`. One Uvicorn worker
is deliberate: multiple Socket.IO workers require a shared Redis manager.

The GitHub workflow runs lint, formatting, and the real-Mongo integration test.
It belongs to this backend repository and has no frontend path filters.

## Every file

| File | Purpose |
| --- | --- |
| `app/main.py` | Request models, HTTP routes, Socket.IO events, and ASGI app |
| `app/auth.py` | Argon2 password hashing, JWT creation/validation, auth dependency |
| `app/db.py` | Async MongoDB connection, indexes, and shared access query |
| `app/__init__.py` | Marks the Python package |
| `tests/conftest.py` | Safe test-only environment defaults |
| `tests/test_app.py` | One end-to-end API, realtime, permission, and persistence check |
| `.github/workflows/ci.yml` | Independent backend CI with a MongoDB service |
| `pyproject.toml` | Runtime/dev dependencies and tool settings |
| `.python-version` | Pins local and hosted Python to 3.12 |
| `.env.example` | Documented environment variables; copy to ignored `.env` |
| `Dockerfile` | Production image and one-worker Uvicorn command |
| `docker-compose.yml` | Local API and durable MongoDB volume |
| `render.yaml` | Render service, health check, and environment contract |
| `.gitignore` | Keeps secrets, caches, and environments out of Git |
| `.dockerignore` | Keeps development files out of the image |
