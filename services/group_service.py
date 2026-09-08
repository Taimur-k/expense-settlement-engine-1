"""
Group Service
==============
Handles group and user management operations.
"""
import logging
import uuid
from typing import Dict, List, Optional

from database.store import get_store, GroupRecord, UserRecord

log = logging.getLogger(__name__)


class GroupService:

    async def create_user(self, name: str, email: str) -> UserRecord:
        store = get_store()
        user = await store.create_user(name=name, email=email)
        log.info("Created user %s (%s).", user.id, email)
        return user

    async def get_user(self, user_id: str) -> Optional[UserRecord]:
        return await get_store().get_user(user_id)

    async def list_users(self) -> List[UserRecord]:
        return await get_store().list_users()

    async def create_group(self, name: str) -> GroupRecord:
        store = get_store()
        group = await store.create_group(name=name)
        log.info("Created group %s (%r).", group.id, name)
        return group

    async def get_group(self, group_id: str) -> Optional[GroupRecord]:
        return await get_store().get_group(group_id)

    async def add_member(self, group_id: str, user_id: str) -> bool:
        store = get_store()
        group = await store.get_group(group_id)
        if not group:
            raise ValueError(f"Group {group_id!r} not found.")
        user = await store.get_user(user_id)
        if not user:
            raise ValueError(f"User {user_id!r} not found.")
        result = await store.add_member(group_id, user_id)
        log.info("Added user %s to group %s.", user_id, group_id)
        return result

    async def get_members(self, group_id: str) -> List[UserRecord]:
        store = get_store()
        group = await store.get_group(group_id)
        if not group:
            return []
        users = []
        for uid in group.member_ids:
            user = await store.get_user(uid)
            if user:
                users.append(user)
        return users


_group_service: Optional[GroupService] = None


def get_group_service() -> GroupService:
    global _group_service
    if _group_service is None:
        _group_service = GroupService()
    return _group_service

