import os

from pymongo import ASCENDING, AsyncMongoClient

client = AsyncMongoClient(
    os.getenv("MONGODB_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=5_000,
)
db = client[os.getenv("MONGODB_DB", "whiteboard")]
users = db.users
boards = db.boards
elements = db.elements


async def ensure_indexes() -> None:
    await users.create_index("email", unique=True)
    await boards.create_index("ownerId")
    await boards.create_index("memberIds")
    await elements.create_index(
        [("boardId", ASCENDING), ("id", ASCENDING)], unique=True
    )


async def find_board(board_id: str, user_id: str, *, owner: bool = False):
    query = {"_id": board_id, "ownerId": user_id}
    if not owner:
        query = {
            "_id": board_id,
            "$or": [{"ownerId": user_id}, {"memberIds": user_id}],
        }
    return await boards.find_one(query)
