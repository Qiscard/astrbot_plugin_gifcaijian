# -*- coding: utf-8 -*-
"""Pre-open size checks, GIF limits, and optional image compression."""
from __future__ import annotations

import io
import os
from typing import Optional, Tuple

from PIL import Image as PILImage
from astrbot.api import logger

from .config_helpers import Cfg


class SafetyError(ValueError):
    """User-facing rejection for oversized / unsafe media."""


def bytes_to_mb(n: int) -> float:
    return n / (1024.0 * 1024.0)


def check_raw_bytes(data: bytes, cfg: Cfg, *, kind: str = "image") -> None:
    """Raise SafetyError if raw payload exceeds configured limits."""
    if not data:
        raise SafetyError("文件为空")
    size = len(data)
    if kind == "video":
        limit = cfg.max_video_size_bytes
        label = "视频"
    elif kind == "gif":
        limit = cfg.max_gif_size_bytes
        label = "GIF"
    else:
        limit = cfg.max_image_size_bytes
        label = "图片"
    # precheck is a hard ceiling before any decode
    pre = cfg.precheck_file_size_bytes
    if size > pre:
        raise SafetyError(
            f"{label}过大 ({bytes_to_mb(size):.1f}MB > 预检上限 {bytes_to_mb(pre):.0f}MB)"
        )
    if size > limit:
        raise SafetyError(
            f"{label}过大 ({bytes_to_mb(size):.1f}MB > {bytes_to_mb(limit):.0f}MB)"
        )


def check_file_path(path: str, cfg: Cfg, *, kind: str = "image") -> None:
    if not path or not os.path.exists(path):
        raise SafetyError("文件不存在")
    try:
        size = os.path.getsize(path)
    except OSError as e:
        raise SafetyError(f"无法读取文件大小: {e}") from e
    # reuse bytes check logic
    if kind == "video":
        limit = cfg.max_video_size_bytes
        label = "视频"
    elif kind == "gif":
        limit = cfg.max_gif_size_bytes
        label = "GIF"
    else:
        limit = cfg.max_image_size_bytes
        label = "图片"
    pre = cfg.precheck_file_size_bytes
    if size > pre:
        raise SafetyError(
            f"{label}过大 ({bytes_to_mb(size):.1f}MB > 预检上限 {bytes_to_mb(pre):.0f}MB)"
        )
    if size > limit:
        raise SafetyError(
            f"{label}过大 ({bytes_to_mb(size):.1f}MB > {bytes_to_mb(limit):.0f}MB)"
        )


def sniff_kind_from_bytes(data: bytes) -> str:
    """Best-effort: gif / video / image."""
    if not data or len(data) < 12:
        return "image"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    # mp4/mov often have ftyp at offset 4
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "video"
    if data[:4] == b"\x1aE\xdf\xa3":  # webm/mkv EBML
        return "video"
    if data[:3] == b"FLV":
        return "video"
    return "image"


def open_image_checked(data: bytes, cfg: Cfg) -> PILImage.Image:
    """Precheck size, open image, enforce pixel / GIF frame limits."""
    kind = sniff_kind_from_bytes(data)
    if kind == "gif":
        check_raw_bytes(data, cfg, kind="gif")
    else:
        check_raw_bytes(data, cfg, kind="image")

    img = PILImage.open(io.BytesIO(data))
    w, h = img.size
    pixels = max(1, w * h)
    max_px = cfg.max_image_pixels
    if pixels > max_px:
        raise SafetyError(
            f"图像像素过多 ({w}x{h} = {pixels} > 上限 {max_px})，可能存在安全风险"
        )

    n_frames = int(getattr(img, "n_frames", 1) or 1)
    is_anim = bool(getattr(img, "is_animated", False)) or n_frames > 1 or kind == "gif"
    if is_anim:
        # n_frames 在部分写出场景下不可靠，必要时 seek 计数
        if n_frames <= 1:
            count = 0
            try:
                while True:
                    img.seek(count)
                    count += 1
                    if count > cfg.max_gif_frames + 1:
                        break
            except EOFError:
                pass
            n_frames = max(1, count)
            try:
                img.seek(0)
            except Exception:
                pass
        if n_frames > cfg.max_gif_frames:
            raise SafetyError(
                f"GIF帧数过多 ({n_frames} > {cfg.max_gif_frames})，可能存在安全风险"
            )
        total = n_frames * pixels
        if total > cfg.max_gif_total_pixels:
            raise SafetyError(
                f"GIF总像素过多 ({n_frames}帧 x {pixels} = {total} > {cfg.max_gif_total_pixels})"
            )
    return img


def compress_image(
    image: PILImage.Image,
    cfg: Cfg,
    *,
    force: bool = False,
) -> Tuple[PILImage.Image, bool]:
    """
    Downscale long edge if enable_auto_compress (or force).
    Returns (image, changed).
    """
    if not force and not cfg.enable_auto_compress:
        return image, False
    max_dim = max(64, int(cfg.max_compress_dimension))
    w, h = image.size
    long_edge = max(w, h)
    if long_edge <= max_dim:
        return image, False
    ratio = max_dim / float(long_edge)
    new_w = max(1, int(round(w * ratio)))
    new_h = max(1, int(round(h * ratio)))
    try:
        out = image.resize((new_w, new_h), PILImage.Resampling.LANCZOS)
        return out, True
    except Exception as e:
        logger.warning(f"[gifcaijian] 压缩失败，使用原图: {e}")
        return image, False


def maybe_compress_bytes_png(data: bytes, cfg: Cfg) -> Tuple[bytes, str]:
    """Open -> optional compress -> PNG bytes. Returns (bytes, note)."""
    if not cfg.enable_auto_compress:
        return data, ""
    try:
        img = PILImage.open(io.BytesIO(data))
        if getattr(img, "is_animated", False) and int(getattr(img, "n_frames", 1) or 1) > 1:
            return data, ""  # animated handled by callers frame-wise
        img.load()
        if img.mode == "P":
            img = img.convert("RGBA")
        compressed, changed = compress_image(img, cfg)
        if not changed:
            return data, ""
        out = io.BytesIO()
        if compressed.mode in ("RGBA", "LA"):
            compressed.save(out, format="PNG", optimize=True)
        else:
            if compressed.mode not in ("RGB", "L"):
                compressed = compressed.convert("RGB")
            compressed.save(out, format="PNG", optimize=True)
        note = f"\n📦 已压缩: {img.size[0]}x{img.size[1]} → {compressed.size[0]}x{compressed.size[1]}"
        return out.getvalue(), note
    except Exception as e:
        logger.warning(f"[gifcaijian] maybe_compress_bytes_png 失败: {e}")
        return data, ""
