"""轻量用户：称呼 + 选填联系方式，不设密码、不验证。

- logs/users.jsonl 追加写，启动回放
- 同称呼视为同一用户，session_count +1
- 联系方式仅服务端与 admin 可见，绝不进入 SSE / 公开快照 / 报告
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any

from pydantic import BaseModel, Field

from . import config


class User(BaseModel):
    user_id: str
    name: str
    contact: str = ""
    created_at: float
    last_seen_at: float | None = None
    session_count: int = 0


class UserStore:
    def __init__(self) -> None:
        self._users: dict[str, User] = {}
        self._loaded = False
        self._lock = threading.Lock()

    def load(self) -> None:
        if self._loaded:
            return
        path = config.USERS_FILE
        if path.exists():
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                        user = User(**payload["user"])
                    except Exception:
                        continue
                    self._users[user.user_id] = user
        self._loaded = True

    def _append(self, user: User) -> None:
        try:
            config.ensure_dirs()
            line = json.dumps(
                {"ts": time.time(), "user": user.model_dump()},
                ensure_ascii=False,
            )
            with self._lock:
                with open(config.USERS_FILE, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError:
            pass

    def create(self, name: str, contact: str = "") -> User:
        """正式注册；调用方需先检查重名。"""
        now = time.time()
        user = User(
            user_id=uuid.uuid4().hex[:10],
            name=name.strip()[:20],
            contact=contact.strip()[:50],
            created_at=now,
            last_seen_at=now,
            session_count=0,
        )
        self._users[user.user_id] = user
        self._append(user)
        return user

    def register(self, name: str, contact: str = "") -> tuple[User, bool]:
        """返回 (user, is_new)。同称呼复用，次数 +1。"""
        name = name.strip()[:20]
        contact = contact.strip()[:50]
        now = time.time()

        for user in self._users.values():
            if user.name == name:
                user.session_count += 1
                user.last_seen_at = now
                if contact:
                    user.contact = contact
                self._append(user)
                return user, False

        user = User(
            user_id=uuid.uuid4().hex[:10],
            name=name,
            contact=contact,
            created_at=now,
            last_seen_at=now,
            session_count=1,
        )
        self._users[user.user_id] = user
        self._append(user)
        return user, True

    def all(self) -> list[User]:
        return list(self._users.values())

    def count(self) -> int:
        return len(self._users)

    def get(self, user_id: str) -> User | None:
        return self._users.get(user_id)

    def find_by_name(self, name: str) -> User | None:
        target = name.strip()
        for user in self._users.values():
            if user.name == target:
                return user
        return None


store = UserStore()
