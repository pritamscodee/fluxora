from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from app.api.deps import ApiProject, DbSession, Pagination
from app.schemas.common import envelope
from app.schemas.video import UploadCreate, UploadOut
from app.services import uploads

router = APIRouter(prefix="/video/uploads", tags=["direct uploads"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_upload(
    body: UploadCreate, project: ApiProject, db: DbSession, request: Request
):
    upload = await uploads.create(db, project, body)
    return envelope(UploadOut.of(upload))


@router.get("")
async def list_uploads(
    project: ApiProject, db: DbSession, page: Pagination, response: Response
):
    rows, total = await uploads.list_for_project(db, project.id, page.limit, page.offset)
    response.headers["X-Total-Count"] = str(total)
    return envelope([UploadOut.of(u) for u in rows])


@router.get("/{upload_id}")
async def get_upload(upload_id: str, project: ApiProject, db: DbSession):
    upload = await uploads.get_for_project(db, project.id, upload_id)
    # A GET is the natural moment to notice the object landed, so a client that polls the
    # upload needs no separate completion call.
    if upload.status == "waiting":
        upload = await uploads.reconcile(db, upload, project)
    return envelope(UploadOut.of(upload))


@router.put("/{upload_id}/cancel")
async def cancel_upload(upload_id: str, project: ApiProject, db: DbSession):
    upload = await uploads.get_for_project(db, project.id, upload_id)
    return envelope(UploadOut.of(await uploads.cancel(db, upload)))


@router.put("/{upload_id}/complete")
async def complete_upload(upload_id: str, project: ApiProject, db: DbSession):
    """Optional fast-path hint from the client after its PUT finishes. Purely an
    optimisation — the reaper reaches the same conclusion on its own."""
    upload = await uploads.get_for_project(db, project.id, upload_id)
    return envelope(UploadOut.of(await uploads.reconcile(db, upload, project)))
