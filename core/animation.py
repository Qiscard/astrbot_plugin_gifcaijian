# -*- coding: utf-8 -*-
"""Animation save helpers (GIF/APNG/WebP)."""
from __future__ import annotations

import io
from typing import List

from PIL import Image as PILImage
from astrbot.api import logger

from .config_helpers import Cfg
from .deps import imageio, np


def save_animation(cfg: Cfg, output: io.BytesIO, frames: list, duration_ms: int, loop: int = 0) -> str:
    """Save frames into output buffer. Returns format used."""
    fmt = cfg.output_format
    if not frames:
        raise ValueError("no frames")

    if fmt == "GIF":
        frames[0].save(
            output,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=duration_ms,
            loop=loop,
            optimize=True,
            disposal=2,
        )
        return "GIF"

    if fmt == "APNG" and imageio is not None and np is not None:
        try:
            writer = imageio.get_writer(output, format="APNG", duration=duration_ms / 1000, loop=loop)
            for frame in frames:
                if frame.mode == "P":
                    frame = frame.convert("RGB")
                writer.append_data(np.array(frame))
            writer.close()
            return "APNG"
        except Exception as e:
            logger.error(f"APNG保存失败，回退到GIF: {e}")
            frames[0].save(
                output,
                format="GIF",
                save_all=True,
                append_images=frames[1:],
                duration=duration_ms,
                loop=loop,
                optimize=True,
                disposal=2,
            )
            return "GIF"

    if fmt == "WEBP":
        frames[0].save(
            output,
            format="WEBP",
            save_all=True,
            append_images=frames[1:],
            duration=duration_ms,
            loop=loop,
            method=3,
            quality=80,
        )
        return "WEBP"

    frames[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=duration_ms,
        loop=loop,
        optimize=True,
        disposal=2,
    )
    return "GIF"
