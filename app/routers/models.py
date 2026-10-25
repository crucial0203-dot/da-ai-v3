"""模型清单（V4.0 新增）：新前端从这里拿全量模型与 provider 状态。

/api/meta 里的 models 是旧前端兼容视图（只列有 Key 的 provider）；
这里回全量：预填未实测的模型 available=False，前端隐藏或置灰并说明原因。
无需登录（与 /api/meta 同级），里面没有任何用户数据。
"""
from __future__ import annotations

from fastapi import APIRouter

from ..services import llm, models

router = APIRouter(prefix="/api", tags=["models"])


@router.get("/models")
def list_models():
    return {
        "models": models.public_list(),
        "default_model": models.default_model_id(),
        "efforts": list(models.EFFORTS),
        "effort_labels": models.EFFORT_LABELS,
        "providers": llm.available_providers(),
    }
