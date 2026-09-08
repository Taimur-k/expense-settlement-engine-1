"""
Users API Routes
================
POST /api/v1/users         - Create user
GET  /api/v1/users         - List all users
GET  /api/v1/users/{id}    - Get user by ID
"""
from datetime import datetime
from typing import List

from fastapi import APIRouter, HTTPException, status

from api.schemas.schemas import CreateUserRequest, UserResponse
from services.group_service import get_group_service

router = APIRouter(prefix="/api/v1/users", tags=["Users"])


def _fmt_user(u) -> UserResponse:
    return UserResponse(
        id         = u.id,
        name       = u.name,
        email      = u.email,
        version    = u.version,
        created_at = u.created_at.isoformat() if isinstance(u.created_at, datetime) else str(u.created_at),
    )


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(body: CreateUserRequest):
    """Create a new user."""
    svc = get_group_service()
    try:
        user = await svc.create_user(name=body.name, email=body.email)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _fmt_user(user)


@router.get("", response_model=List[UserResponse])
async def list_users():
    """List all users."""
    svc   = get_group_service()
    users = await svc.list_users()
    return [_fmt_user(u) for u in users]


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(user_id: str):
    """Get a user by ID."""
    svc  = get_group_service()
    user = await svc.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail=f"User {user_id!r} not found.")
    return _fmt_user(user)

