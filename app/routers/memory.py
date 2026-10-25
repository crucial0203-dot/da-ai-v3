"""V4.0 N7 记忆路由。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import deps
from ..services import memory

router = APIRouter(prefix="/api/memory", tags=["memory"])


class MemoryIn(BaseModel):
    content: str = Field(min_length=1, max_length=1000)
    kind: str = Field(default="note", max_length=32)


class MemoryPatch(BaseModel):
    content: str = Field(min_length=1, max_length=1000)


@router.get("")
def list_memories(request: Request):
    user = deps.current_user(request)
    return {"memories": memory.list_memories(user["id"])}


@router.post("")
def create_memory(payload: MemoryIn, request: Request):
    user = deps.current_user(request)
    try:
        mem_id, _change = memory.add_memory(user["id"], payload.content, kind=payload.kind, source="user")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "id": mem_id}


@router.patch("/{mem_id}")
def update_memory(mem_id: str, payload: MemoryPatch, request: Request):
    user = deps.current_user(request)
    try:
        memory.update_memory(user["id"], mem_id, payload.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


@router.delete("/{mem_id}")
def delete_memory(mem_id: str, request: Request):
    user = deps.current_user(request)
    try:
        memory.delete_memory(user["id"], mem_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}
