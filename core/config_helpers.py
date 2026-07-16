"""Config accessors with defaults."""
from __future__ import annotations
from typing import Any, Dict


class Cfg:
    def __init__(self, raw: Dict[str, Any] | None = None):
        self.raw = raw or {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    @property
    def output_format(self) -> str:
        fmt = str(self.get("output_format", "GIF")).upper()
        return fmt if fmt in ("GIF", "APNG", "WEBP") else "GIF"

    @property
    def default_scale(self) -> float:
        return float(self.get("default_scale", 0.3))

    @property
    def default_fps(self) -> int:
        return int(self.get("default_fps", 10))

    @property
    def max_gif_duration(self) -> float:
        return float(self.get("max_gif_duration", 10.0))

    @property
    def max_video_size_mb(self) -> float:
        return float(self.get("max_video_size_mb", 50.0))

    @property
    def max_download_size_mb(self) -> float:
        return float(self.get("max_download_size_mb", 50.0))

    @property
    def gif_max_colors(self) -> int:
        return int(self.get("gif_max_colors", 256))

    @property
    def crop_output_format(self) -> str:
        fmt = str(self.get("crop_output_format", "PNG")).upper()
        return "JPEG" if fmt in ("JPEG", "JPG") else "PNG"

    @property
    def max_concurrent_tasks(self) -> int:
        return int(self.get("max_concurrent_tasks", 2))

    @property
    def task_timeout_sec(self) -> float:
        return float(self.get("task_timeout_sec", 120.0))

    @property
    def max_queue_waiting(self) -> int:
        return int(self.get("max_queue_waiting", 8))
