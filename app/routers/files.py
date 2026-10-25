"""上传、下载、材料预览。"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response

from .. import config, db, deps
from ..services import extract, models, vision

router = APIRouter(prefix="/api", tags=["files"])

MAGIC = {
    ".pdf": (b"%PDF",),
    ".docx": (b"PK\x03\x04",),
    ".pptx": (b"PK\x03\x04",),
    ".zip": (b"PK\x03\x04",),
    ".png": (b"\x89PNG",),
    ".jpg": (b"\xff\xd8",),
    ".jpeg": (b"\xff\xd8",),
    ".webp": (b"RIFF",),
}

MIME = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".zip": "application/zip",
}


def _check_magic(ext: str, head: bytes) -> bool:
    if ext == ".webp":
        # RIFF 只是容器头，wav/avi 也符合；必须同时看第 8-12 字节是不是 WEBP
        return head.startswith(b"RIFF") and head[8:12] == b"WEBP"
    expected = MAGIC.get(ext)
    if not expected:
        return True
    return any(head.startswith(sig) for sig in expected)


def _task_params(task) -> dict:
    try:
        return json.loads(task["params_json"] or "{}")
    except (json.JSONDecodeError, KeyError, TypeError):
        return {}


@router.post("/tasks/{task_id}/files")
async def upload(task_id: str, request: Request, files: list[UploadFile] = File(...)):
    user = deps.current_user(request)
    task = db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="无权向这个任务上传文件")

    if len(files) > config.MAX_BATCH_FILES:
        raise HTTPException(
            status_code=413,
            detail=f"一次最多传 {config.MAX_BATCH_FILES} 个文件，分批来",
        )

    saved = []
    for upload_file in files:
        name = Path(upload_file.filename or "未命名").name
        ext = Path(name).suffix.lower()
        if ext not in config.ALLOWED_UPLOAD_EXT:
            raise HTTPException(status_code=400, detail=f"不支持的文件类型：{ext or '无扩展名'}")

        is_image = ext in config.IMAGE_EXT
        vision_ok = False
        if is_image:
            # 纯文本模型收到图片不会报错，只会静默丢掉再拿文字硬猜。
            # 与其让用户拿到一个"看起来答了但没看图"的结果，不如在这里直接拦住。
            spec = models.resolve_model(None, _task_params(task), user).spec
            if not spec.vision:
                raise HTTPException(status_code=400, detail={
                    "message": (
                        f"当前模型「{spec.label}」看不了图片。"
                        f"先在输入框左下角切到 M3.1-Flash 或 M3，再传图片。"
                    ),
                    "code": "vision_unsupported",
                    "model": spec.id,
                })
            if len(vision.image_rows(task_id)) >= config.MAX_IMAGES_PER_TASK:
                raise HTTPException(status_code=400, detail={
                    "message": (
                        f"每个任务最多 {config.MAX_IMAGES_PER_TASK} 张图片，"
                        f"先在右侧材料里删掉旧的再加。"
                    ),
                    "code": "too_many_images",
                })
            vision_ok = True

        limit = config.MAX_IMAGE_UPLOAD_BYTES if is_image else config.MAX_UPLOAD_BYTES
        file_id = db.new_id("f_")
        target_dir = config.FILES_DIR / user["id"]
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{file_id}{ext}"

        hasher = hashlib.sha256()
        size = 0
        head = b""
        with open(target, "wb") as out:
            while True:
                chunk = await upload_file.read(1024 * 256)
                if not chunk:
                    break
                if not head:
                    head = chunk[:16]
                size += len(chunk)
                if size > limit:
                    out.close()
                    target.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"「{name}」超过 {limit // (1024 * 1024)}MB 上限",
                    )
                hasher.update(chunk)
                out.write(chunk)

        if not _check_magic(ext, head):
            target.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=f"「{name}」的真实格式与扩展名不符，已拒绝")

        if ext == ".zip":
            try:
                with zipfile.ZipFile(target) as archive:
                    count = len(archive.infolist())
                    total = sum(i.file_size for i in archive.infolist())
                if count > config.MAX_ZIP_ENTRIES or total > config.MAX_ZIP_UNCOMPRESSED:
                    target.unlink(missing_ok=True)
                    raise HTTPException(status_code=400, detail="压缩包过大或条目过多，已拒绝")
            except zipfile.BadZipFile:
                target.unlink(missing_ok=True)
                raise HTTPException(status_code=400, detail="压缩包已损坏")

        info = extract.extract(target, name)
        sidecar = config.DATA_DIR / "extracted" / f"{file_id}.txt"
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        if info.get("text"):
            sidecar.write_text(info["text"], encoding="utf-8")

        meta = {
            "kind": info.get("kind"),
            "page_count": info.get("page_count"),
            "chars": info.get("chars", 0),
            "has_text": info.get("has_text", False),
            "image": bool(info.get("image")),
            "note": info.get("note", ""),
        }
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO files(id, user_id, task_id, kind, name, path, mime, size, sha256, meta_json, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (file_id, user["id"], task_id, "source", name, str(target),
                 MIME.get(ext, "application/octet-stream"), size, hasher.hexdigest(),
                 json.dumps(meta, ensure_ascii=False), db.now_iso()),
            )
        saved.append({
            "id": file_id, "name": name, "size": size, "meta": meta,
            "kind": "source",
            "thumb_url": f"/api/files/{file_id}/thumb" if is_image else "",
            # 前端据此判断"这张图当前模型读不读得到"，不再拿 has_text 当失败依据
            "vision_usable": vision_ok,
        })

    db.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (db.now_iso(), task_id))
    return {"ok": True, "files": saved}


@router.get("/files")
def list_files(request: Request, kind: str | None = None):
    user = deps.current_user(request)
    sql = (
        "SELECT f.id, f.kind, f.name, f.size, f.created_at, f.task_id, f.meta_json, t.title AS task_title "
        "FROM files f LEFT JOIN tasks t ON t.id = f.task_id WHERE f.user_id = ?"
    )
    params: list = [user["id"]]
    if kind in {"source", "deliverable"}:
        sql += " AND f.kind = ?"
        params.append(kind)
    sql += " ORDER BY f.created_at DESC LIMIT 300"
    rows = db.rows(sql, params)
    return {
        "files": [
            {
                "id": r["id"], "kind": r["kind"], "name": r["name"], "size": r["size"],
                "created_at": r["created_at"], "task_id": r["task_id"], "task_title": r["task_title"],
                "meta": json.loads(r["meta_json"] or "{}"),
            }
            for r in rows
        ]
    }


@router.get("/files/{file_id}")
def download(file_id: str, request: Request):
    user = deps.current_user(request)
    row = db.one("SELECT * FROM files WHERE id = ?", (file_id,))
    if not row:
        raise HTTPException(status_code=404, detail="文件不存在")
    if row["user_id"] != user["id"] and user["role"] != "admin":
        raise HTTPException(status_code=403, detail="无权下载这个文件")
    path = Path(row["path"])
    if not path.exists():
        raise HTTPException(status_code=410, detail="文件已被清理，请重新生成")
    return FileResponse(
        path,
        media_type=row["mime"] or "application/octet-stream",
        filename=row["name"],
        headers={"Cache-Control": "private, max-age=600"},
    )


@router.get("/files/{file_id}/preview")
def preview(file_id: str, request: Request):
    user = deps.current_user(request)
    row = db.one("SELECT * FROM files WHERE id = ?", (file_id,))
    if not row or row["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="文件不存在")
    sidecar = config.DATA_DIR / "extracted" / f"{file_id}.txt"
    text = sidecar.read_text(encoding="utf-8", errors="replace") if sidecar.exists() else ""
    meta = json.loads(row["meta_json"] or "{}")
    return {
        "id": file_id,
        "name": row["name"],
        "meta": meta,
        "text_head": text[:4000],
        "chars": len(text),
    }


@router.get("/files/{file_id}/thumb")
def thumb(file_id: str, request: Request):
    """现算缩略图，不落盘：原图删了缩略图自然没了，不会留下孤儿文件。"""
    user = deps.current_user(request)
    row = db.one("SELECT * FROM files WHERE id = ?", (file_id,))
    if not row or row["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="文件不存在")
    path = Path(row["path"])
    if path.suffix.lower() not in config.IMAGE_EXT or not path.is_file():
        raise HTTPException(status_code=404, detail="不是图片")
    try:
        from PIL import Image, ImageOps

        with Image.open(path) as image:
            rgb = ImageOps.exif_transpose(image)
            if rgb.mode != "RGB":
                rgba = rgb.convert("RGBA")
                canvas = Image.new("RGB", rgba.size, (255, 255, 255))
                canvas.paste(rgba, mask=rgba.split()[-1])
                rgb = canvas
            rgb.thumbnail((240, 240))
            buffer = io.BytesIO()
            rgb.save(buffer, "JPEG", quality=80, optimize=True)
    except Exception:
        raise HTTPException(status_code=415, detail="这张图解码失败")
    return Response(
        buffer.getvalue(),
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.delete("/files/{file_id}")
def delete_file(file_id: str, request: Request):
    user = deps.current_user(request)
    row = db.one("SELECT * FROM files WHERE id = ?", (file_id,))
    if not row:
        raise HTTPException(status_code=404, detail="文件不存在")
    if row["user_id"] != user["id"] and user["role"] != "admin":
        raise HTTPException(status_code=403, detail="无权删除这个文件")
    with db.tx() as conn:
        conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
    try:
        resolved = Path(row["path"]).resolve()
        if config.DATA_DIR in resolved.parents:
            resolved.unlink(missing_ok=True)
        sidecar = (config.DATA_DIR / "extracted" / f"{file_id}.txt")
        sidecar.unlink(missing_ok=True)
    except Exception:
        pass
    return {"ok": True}
