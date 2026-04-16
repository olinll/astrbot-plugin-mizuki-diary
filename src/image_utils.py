"""图片处理：识别格式、拒绝 GIF、统一转换为 WebP。"""

from __future__ import annotations

import io
from pathlib import Path


class ImageError(Exception):
    pass


def to_webp(data: bytes, quality: int = 85) -> bytes:
    """把任意格式的图片字节转成 WebP 字节。GIF 直接报错。"""
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        raise ImageError(f"无法识别图片: {e}")

    fmt = (img.format or "").upper()
    if fmt == "GIF":
        raise ImageError("不支持 GIF 格式，请发送 JPG/PNG/WebP 等静态图片。")

    if img.mode not in ("RGB", "RGBA"):
        if "A" in img.mode or img.info.get("transparency") is not None:
            img = img.convert("RGBA")
        else:
            img = img.convert("RGB")

    out = io.BytesIO()
    img.save(out, format="WEBP", quality=max(1, min(100, quality)))
    return out.getvalue()


async def extract_image_bytes(img_comp) -> bytes:
    """从 AstrBot 的 Image 消息段提取原始字节。"""
    path = await img_comp.convert_to_file_path()
    if not path:
        raise ImageError("无法获取图片文件路径")
    return Path(path).read_bytes()
