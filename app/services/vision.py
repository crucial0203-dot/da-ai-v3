"""图片 → Anthropic image block。

三条硬约束，写在代码里别改：

1. **读盘只发生在 `_encode()`**，且路径必须落在 `config.FILES_DIR` 之内 ——
   DB 里的 `path` 列虽然是自己写的，但一旦被篡改就是任意文件读取。
2. **base64 永不落库**：不写 `messages` 表、不写 sidecar、不落临时文件。
   每次请求现读现编码，代价是要重新算，收益是库不会膨胀、删文件即失效。
3. **能直传就直传**：长边 ≤ 1568 且体积 ≤ 1.2MB 的原图不重编码（重编码会掉画质，
   对小截图没必要）；超了就缩放转 JPEG q85，控 token 也控请求体。

`build_blocks()` 是唯一对外的组装入口，调用方必须用 `asyncio.to_thread` 跑它：
Pillow 是 CPU 活，直接在事件循环里做会卡住所有 SSE 流。
"""
from __future__ import annotations

import base64
import io
from pathlib import Path

from .. import config, db

IMAGE_EXT = config.IMAGE_EXT
_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


def image_rows(task_id: str) -> list:
    """任务下的图片材料。只查库不读盘，便宜，可以在预扣额度之前调用。"""
    rows = db.rows(
        "SELECT id, name, path, mime, size FROM files "
        "WHERE task_id = ? AND kind = 'source' ORDER BY created_at ASC",
        (task_id,),
    )
    out = []
    for row in rows:
        try:
            suffix = Path(row["path"]).suffix.lower()
        except (TypeError, ValueError):
            continue
        if suffix in IMAGE_EXT:
            out.append(row)
    return out


def _safe_path(raw: str) -> Path | None:
    try:
        path = Path(raw).resolve()
    except (OSError, ValueError):
        return None
    base = config.FILES_DIR.resolve()
    if base != path.parent and base not in path.parents:
        return None
    return path if path.is_file() else None


def _to_rgb(image):
    """转 RGB 并把透明区域铺白：直接 convert 会让透明像素变黑。"""
    from PIL import Image

    if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
        canvas = Image.new("RGB", image.size, (255, 255, 255))
        rgba = image.convert("RGBA")
        canvas.paste(rgba, mask=rgba.split()[-1])
        return canvas
    return image.convert("RGB")


def _encode(path: Path) -> tuple[str, str] | None:
    """返回 (media_type, base64)；解不了就返回 None，由调用方计入 dropped。"""
    from PIL import Image, ImageOps

    try:
        size = path.stat().st_size
        with Image.open(path) as image:
            # 先取 format 再 transpose：exif_transpose 返回的是副本，format 会丢，
            # 拿不到就会把本来能直传的小 PNG 白白重编码成 JPEG（截图掉画质）。
            fmt = image.format
            image = ImageOps.exif_transpose(image)
            media_type = _FORMATS.get(fmt or "")
            small = max(image.size) <= config.VISION_MAX_EDGE
            if media_type and small and size <= config.VISION_SEND_RAW_BYTES:
                return media_type, base64.b64encode(path.read_bytes()).decode("ascii")

            rgb = _to_rgb(image)
            rgb.thumbnail((config.VISION_MAX_EDGE, config.VISION_MAX_EDGE))
            buffer = io.BytesIO()
            rgb.save(buffer, "JPEG", quality=config.VISION_JPEG_QUALITY, optimize=True)
            return "image/jpeg", base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception:
        return None


def build_blocks(rows: list, limit: int | None = None) -> tuple[list[dict], dict]:
    """把图片行组装成 image block 列表 + 统计。

    统计里的 `dropped` 必须回给前端和模型：悄悄少发图、又不说，就是让模型瞎猜。
    """
    limit = limit if limit is not None else config.MAX_IMAGES_PER_TURN
    blocks: list[dict] = []
    names: list[str] = []
    dropped = max(0, len(rows) - limit)
    total_b64 = 0

    for row in rows[:limit]:
        path = _safe_path(row["path"])
        encoded = _encode(path) if path else None
        if not encoded:
            dropped += 1
            continue
        media_type, data = encoded
        if total_b64 + len(data) > config.MAX_VISION_B64_TOTAL:
            dropped += 1
            continue
        total_b64 += len(data)
        blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        })
        names.append(row["name"])

    return blocks, {
        "sent": len(blocks),
        "dropped": dropped,
        "names": names,
        "b64_bytes": total_b64,
    }


def note_text(stats: dict) -> str:
    """放在文字前面的一行说明。让模型知道图从哪来、有几张、有没有少。"""
    if not stats.get("sent"):
        return ""
    head = (
        f"（本轮附了 {stats['sent']} 张图片：{'、'.join(stats['names'])}。"
        f"先看清图里的内容再回答；图里没有的信息不要编。）"
    )
    if stats.get("dropped"):
        head += f"（另有 {stats['dropped']} 张未发送：超出单轮上限或无法解码。）"
    return head + "\n"
