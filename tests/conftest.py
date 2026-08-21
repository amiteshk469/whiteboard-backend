import os

os.environ.setdefault("JWT_SECRET", "test-secret-that-is-at-least-32-characters")
os.environ.setdefault("MONGODB_DB", "whiteboard_test")
