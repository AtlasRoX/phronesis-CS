"""
Research projects and their memory (feature 16).

A project groups threads; its memory is a bi-temporal fact graph (Zep, arXiv:2501.13956) written by the
project-memory phase from verified tool results (Mem0-style ADD/UPDATE/INVALIDATE/NOOP, arXiv:2504.19413) and
recalled with Personalized PageRank (HippoRAG, arXiv:2405.14831) plus recency x importance x relevance
(Generative Agents, arXiv:2304.03442). See phronesis/utils/memory/project_store.py.

Routes (all scoped to the authenticated user; another user's project is a 404):
- POST /api/projects {name, description}, GET /api/projects
- GET / PATCH / DELETE /api/projects/{id}
- GET /api/projects/{id}/memory?query=&include_invalid=&limit=
- PATCH /api/projects/{id}/memory/{fact_id} (invalidate, or edit: a changed object/value is a new version)
- DELETE /api/projects/{id}/memory/{fact_id}
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from app.backend.auth import get_current_user
from phronesis.utils.memory.project_store import (
    FACT_ID_RE,
    PROJECT_ID_RE,
    ProjectStoreError,
    get_project_store,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])

ProjectIdPath = Path(..., min_length=4, max_length=64)
FactIdPath = Path(..., min_length=4, max_length=64)


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)


class ProjectPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)


class FactPatch(BaseModel):
    invalidate: bool = Field(default=False, description="Mark the fact no longer valid (kept in the history)")
    reason: str = Field(default="", max_length=300)
    object: Optional[str] = Field(default=None, min_length=1, max_length=500)
    value: Optional[str] = Field(default=None, max_length=64)
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    verified: Optional[bool] = None
    tags: Optional[List[str]] = Field(default=None, max_length=10)


def _check_project(project_id: str) -> None:
    if not PROJECT_ID_RE.match(project_id or ""):
        raise HTTPException(status_code=404, detail="Project not found")


def _check_fact(fact_id: str) -> None:
    if not FACT_ID_RE.match(fact_id or ""):
        raise HTTPException(status_code=404, detail="Fact not found")


@router.post("", status_code=201)
async def create_project(req: ProjectCreate, user_id: str = Depends(get_current_user)) -> Dict[str, Any]:
    try:
        return await asyncio.to_thread(get_project_store().create_project, user_id, req.name, req.description)
    except ProjectStoreError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("")
async def list_projects(limit: int = Query(default=100, ge=1, le=500), user_id: str = Depends(get_current_user)) -> Dict[str, Any]:
    projects = await asyncio.to_thread(get_project_store().list_projects, user_id, limit)
    return {"projects": projects, "count": len(projects)}


@router.get("/{project_id}")
async def get_project(project_id: str = ProjectIdPath, user_id: str = Depends(get_current_user)) -> Dict[str, Any]:
    _check_project(project_id)
    project = await asyncio.to_thread(get_project_store().get_project, project_id, user_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.patch("/{project_id}")
async def patch_project(req: ProjectPatch, project_id: str = ProjectIdPath, user_id: str = Depends(get_current_user)) -> Dict[str, Any]:
    _check_project(project_id)
    try:
        project = await asyncio.to_thread(get_project_store().update_project, project_id, user_id, req.name, req.description)
    except ProjectStoreError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.delete("/{project_id}")
async def delete_project(project_id: str = ProjectIdPath, user_id: str = Depends(get_current_user)) -> Dict[str, Any]:
    _check_project(project_id)
    if not await asyncio.to_thread(get_project_store().delete_project, project_id, user_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return {"success": True, "project_id": project_id}


@router.get("/{project_id}/memory")
async def project_memory(
    project_id: str = ProjectIdPath,
    query: str = Query(default="", max_length=2000),
    include_invalid: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=500),
    user_id: str = Depends(get_current_user),
) -> Dict[str, Any]:
    """Facts of a project: ranked for `query` (the recall the agent would get), else the newest first."""
    _check_project(project_id)
    store = get_project_store()
    if not await asyncio.to_thread(store.owns, project_id, user_id):
        raise HTTPException(status_code=404, detail="Project not found")
    if query.strip():
        facts = await asyncio.to_thread(store.recall, project_id, user_id, query, (), limit, False)
        mode = "ranked"
    else:
        facts = await asyncio.to_thread(store.list_facts, project_id, user_id, include_invalid, limit)
        mode = "recent"
    return {"project_id": project_id, "mode": mode, "facts": facts, "count": len(facts)}


@router.patch("/{project_id}/memory/{fact_id}")
async def patch_fact(req: FactPatch, project_id: str = ProjectIdPath, fact_id: str = FactIdPath, user_id: str = Depends(get_current_user)) -> Dict[str, Any]:
    _check_project(project_id)
    _check_fact(fact_id)
    store = get_project_store()
    try:
        if req.invalidate:
            fact = await asyncio.to_thread(store.invalidate_fact, project_id, fact_id, user_id, req.reason or "invalidated by the researcher")
        else:
            fact = await asyncio.to_thread(
                lambda: store.edit_fact(
                    project_id, fact_id, user_id, object=req.object, value=req.value, confidence=req.confidence, verified=req.verified, tags=req.tags
                )
            )
    except ProjectStoreError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if fact is None:
        raise HTTPException(status_code=404, detail="Fact not found")
    return fact


@router.delete("/{project_id}/memory/{fact_id}")
async def delete_fact(project_id: str = ProjectIdPath, fact_id: str = FactIdPath, user_id: str = Depends(get_current_user)) -> Dict[str, Any]:
    _check_project(project_id)
    _check_fact(fact_id)
    if not await asyncio.to_thread(get_project_store().delete_fact, project_id, fact_id, user_id):
        raise HTTPException(status_code=404, detail="Fact not found")
    return {"success": True, "fact_id": fact_id}
