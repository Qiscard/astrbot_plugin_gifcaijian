"""Optional dependency detection."""
from astrbot.api import logger

try:
    import imageio  # type: ignore
except ImportError:
    imageio = None

try:
    import numpy as np  # type: ignore
except ImportError:
    np = None


def warn_missing_deps() -> None:
    if imageio is None:
        logger.warning(
            "插件[astrbot_plugin_gifcaijian]缺少 imageio，请执行: pip install imageio[ffmpeg] numpy"
        )
    if np is None:
        logger.warning(
            "插件[astrbot_plugin_gifcaijian]缺少 numpy，请执行: pip install numpy"
        )


def has_imageio() -> bool:
    return imageio is not None


def has_numpy() -> bool:
    return np is not None
