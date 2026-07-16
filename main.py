# -*- coding: utf-8 -*-
"""
astrbot_plugin_gifcaijian - GIF/动图/裁剪工具箱

原作者: shskjw
维护/二次开发: Qiscard
"""
from __future__ import annotations

import io
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.all import *
from astrbot.api.event import filter

from .core.config_helpers import Cfg
from .core.crop import CropEngine
from .core.deps import has_imageio, warn_missing_deps
from .core.media_io import MediaHelper
from .core.processors import Processors
from .core.task_queue import TaskQueue

PLUGIN_VERSION = "1.7.0"
ORIGINAL_AUTHOR = "shskjw"
CURRENT_AUTHOR = "Qiscard"
REPO_URL = "https://github.com/Qiscard/astrbot_plugin_gifcaijian"


@register(
    "astrbot_plugin_gifcaijian",
    CURRENT_AUTHOR,
    f"GIF/动图工具箱：视频转GIF、智能裁剪、合成变速等 (原作者 {ORIGINAL_AUTHOR})",
    PLUGIN_VERSION,
    REPO_URL,
)
class SpriteToGifPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.cfg = Cfg(config if config is not None else {})
        self.media = MediaHelper(self.cfg)
        self.crop = CropEngine(self.cfg)
        self.proc = Processors(self.cfg)
        self.queue = TaskQueue(
            max_concurrent=self.cfg.max_concurrent_tasks,
            default_timeout=self.cfg.task_timeout_sec,
            max_waiting=self.cfg.max_queue_waiting,
        )
        warn_missing_deps()
        logger.info(
            f"[gifcaijian] v{PLUGIN_VERSION} by {CURRENT_AUTHOR} "
            f"(原作者 {ORIGINAL_AUTHOR}) | {self.queue.status_text}"
        )

    async def terminate(self):
        pass

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _current_text(self, event: AstrMessageEvent) -> str:
        if hasattr(event, "message_str") and event.message_str:
            return event.message_str
        parts = []
        if hasattr(event.message_obj, "message") and isinstance(event.message_obj.message, list):
            for seg in event.message_obj.message:
                if isinstance(seg, Comp.Plain) and seg.text:
                    parts.append(seg.text)
        return " ".join(parts)

    def _img_from_bytes(self, data: bytes, suffix: str = "out.gif"):
        try:
            tmp_dir = Path(tempfile.gettempdir()) / "astrbot_gifcaijian"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            p = tmp_dir / suffix
            p.write_bytes(data)
            return Comp.Image.fromFileSystem(str(p))
        except Exception:
            return Comp.Image.fromBytes(data)

    async def _run_task(self, name: str, factory, timeout: float = None):
        return await self.queue.run(factory, timeout=timeout, name=name)

    async def _run_cpu(self, name: str, func, *args, timeout: float = None, **kwargs):
        return await self.queue.run_sync(func, *args, timeout=timeout, name=name, **kwargs)

    def _help_text(self) -> str:
        fmt = self.cfg.output_format
        return f"""📦 GIF工具箱 v{PLUGIN_VERSION} 帮助
维护: {CURRENT_AUTHOR} | 原作者: {ORIGINAL_AUTHOR}
输出: {fmt} | 队列: {self.queue.status_text}

━━━ 🎬 动图 ━━━
• 视频转gif [开始/时长/fps/缩放]
• /g加速 [倍数]  /  /g减速 [倍数]
• gif分解
• 合成gif / 合成1gif / 合成2gif [行]x[列] [间隔] [边距]
• 多图合成gif [帧间隔秒]

━━━ ✂️ 裁剪 ━━━
• 自动裁切 [阈值] [模式] [降噪N]
• 裁剪 [行]x[列] [边距]                 ← 纯均分
• 智能裁剪 [行]x[列] [边距] [阈值]       ← 内容缝(抗AI九宫格串行)
• 批量去白边 [阈值]

━━━ 🎨 特效 ━━━
• 图片转线稿
• 表情包做旧 [次数]

━━━ ⚙️ 配置 ━━━
output_format={fmt} max_gif_duration={self.cfg.max_gif_duration}s
default_scale={self.cfg.default_scale} default_fps={self.cfg.default_fps}
max_concurrent_tasks={self.cfg.max_concurrent_tasks} task_timeout_sec={self.cfg.task_timeout_sec}

💡 多数指令需回复图片/视频
"""

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------
    @filter.command("gif帮助")
    async def show_help(self, event: AstrMessageEvent):
        yield event.plain_result(self._help_text())

    @filter.command("自动裁切")
    async def auto_crop(self, event: AstrMessageEvent):
        msg_text = event.message_str.replace("自动裁切", "").strip()
        threshold, mode, denoise = 240, "auto", 3
        denoise_match = re.search(r"(?:降噪|denoise)\s*(\d)", msg_text, re.I)
        if denoise_match:
            denoise = max(1, min(int(denoise_match.group(1)), 7)) | 1
            msg_text = msg_text.replace(denoise_match.group(0), " ")
        num_match = re.search(r"(\d+)", msg_text)
        if num_match:
            threshold = max(0, min(int(num_match.group(1)), 255))
        if "white" in msg_text.lower():
            mode = "white"
        elif "transparent" in msg_text.lower():
            mode = "transparent"

        if not self.media._get_image_url(event) and self.media._get_first_image_component(event) is None:
            yield event.plain_result("❌ 请发送或回复图片\n用法: 自动裁切 [阈值] [模式] [降噪N]")
            return

        yield event.plain_result(f"⏳ 自动裁切中 (阈值:{threshold}, 模式:{mode}, 降噪:{denoise})...")
        try:
            img_data = await self._run_task(
                "download_image",
                lambda: self.media._resolve_image_bytes(event),
                timeout=60,
            )
            if not img_data:
                yield event.plain_result("❌ 图片下载失败")
                return
            result_bytes, info = await self._run_cpu(
                "auto_crop",
                self.crop._worker_auto_crop,
                img_data,
                threshold,
                mode,
                denoise,
                timeout=90,
            )
        except Exception as e:
            yield event.plain_result(f"❌ {e}")
            return

        if result_bytes:
            yield event.chain_result([Comp.Plain(info), Comp.Image.fromBytes(result_bytes)])
        else:
            yield event.plain_result(info)

    @filter.command("裁剪")
    async def crop_and_forward(self, event: AstrMessageEvent):
        """原版均分网格裁剪。"""
        text = self._current_text(event).replace("裁剪", "", 1)
        text, margins = self.crop._parse_margins(text)
        m = re.search(r"(\d+)\s*[xX×]\s*(\d+)", text)
        if not m:
            yield event.plain_result("❌ 请指定网格，如: 裁剪 3x3")
            return
        rows, cols = int(m.group(1)), int(m.group(2))
        if rows < 1 or cols < 1 or rows * cols > 100:
            yield event.plain_result("❌ 行列不合法 (总数≤100)")
            return

        yield event.plain_result(f"⏳ 均分裁剪 {rows}x{cols} ...")
        try:
            img_data = await self._run_task("download_image", lambda: self.media._resolve_image_bytes(event), timeout=60)
            if not img_data:
                yield event.plain_result("❌ 图片下载失败")
                return
            msg, bytes_list = await self._run_cpu(
                "crop_grid_equal",
                self.crop._worker_crop_grid,
                img_data,
                margins,
                rows,
                cols,
                timeout=90,
            )
        except Exception as e:
            yield event.plain_result(f"❌ {e}")
            return

        if not bytes_list:
            yield event.plain_result(msg)
            return
        nodes = [Comp.Node(name="裁剪(均分)", content=[Comp.Plain(f"结果 {rows}x{cols}{msg}")])]
        for b in bytes_list[:50]:
            nodes.append(Comp.Node(name="裁剪(均分)", content=[Comp.Image.fromBytes(b)]))
        yield event.chain_result([Comp.Nodes(nodes=nodes)])

    @filter.command("智能裁剪")
    async def smart_crop(self, event: AstrMessageEvent):
        """智能网格裁剪：内容聚类找缝 + 格内去白边（抗AI九宫格串行）。"""
        text = self._current_text(event).replace("智能裁剪", "", 1)
        text, margins = self.crop._parse_margins(text)
        m = re.search(r"(\d+)\s*[xX×]\s*(\d+)", text)
        if not m:
            yield event.plain_result("❌ 请指定网格，如: 智能裁剪 3x3")
            return
        rows, cols = int(m.group(1)), int(m.group(2))
        if rows < 1 or cols < 1 or rows * cols > 100:
            yield event.plain_result("❌ 行列不合法（总数≤100）")
            return

        threshold = 248
        denoise = 3
        denoise_match = re.search(r"(?:降噪|denoise)\s*(\d)", text, re.I)
        if denoise_match:
            denoise = max(1, min(int(denoise_match.group(1)), 7)) | 1
            text = text.replace(denoise_match.group(0), " ")
        nums = re.findall(r"\b(\d{2,3})\b", text)
        for n in nums:
            v = int(n)
            if 150 <= v <= 255:
                threshold = v
                break
        auto_clean = "不去白边" not in text

        yield event.plain_result(f"⏳ 智能裁剪 {rows}x{cols} ...")
        try:
            img_data = await self._run_task(
                "download_image",
                lambda: self.media._resolve_image_bytes(event),
                timeout=60,
            )
            if not img_data:
                yield event.plain_result("❌ 图片下载失败")
                return
            report, bytes_list, _crop_msg = await self._run_cpu(
                "smart_crop",
                self.crop.smart_grid_split,
                img_data,
                rows,
                cols,
                margins,
                threshold,
                auto_clean,
                min(threshold, 240),
                denoise,
                timeout=120,
            )
        except Exception as e:
            yield event.plain_result(f"❌ {e}")
            return

        if not bytes_list:
            yield event.plain_result(report)
            return

        nodes = [Comp.Node(name="智能裁剪", content=[Comp.Plain(report)])]
        for i, b in enumerate(bytes_list[:50]):
            nodes.append(
                Comp.Node(
                    name="智能裁剪",
                    content=[Comp.Plain(f"第{i+1}张"), Comp.Image.fromBytes(b)],
                )
            )
        if len(bytes_list) > 50:
            nodes.append(
                Comp.Node(
                    name="智能裁剪",
                    content=[Comp.Plain(f"⚠️ 共{len(bytes_list)}张，仅显示前50张")],
                )
            )
        yield event.chain_result([Comp.Nodes(nodes=nodes)])

    @filter.command("图片转线稿")
    async def img_to_line_art(self, event: AstrMessageEvent):
        yield event.plain_result("⏳ 转线稿中...")
        try:
            img_bytes = await self._run_task("download_image", lambda: self.media._resolve_image_bytes(event), timeout=60)
            if not img_bytes:
                yield event.plain_result("❌ 图片下载失败")
                return
            result_bytes = await self._run_cpu("line_art", self.proc._worker_local_line_art, img_bytes, timeout=60)
        except Exception as e:
            yield event.plain_result(f"❌ {e}")
            return
        if result_bytes:
            yield event.chain_result([Comp.Plain("✅ 转换成功"), Comp.Image.fromBytes(result_bytes)])
        else:
            yield event.plain_result("❌ 转换失败")

    @filter.command("视频转gif")
    async def video_to_gif_cmd(self, event: AstrMessageEvent):
        if not has_imageio():
            yield event.plain_result("❌ 缺少 imageio，请 pip install imageio[ffmpeg] numpy")
            return

        text = self._current_text(event)
        # strip command word
        text = re.sub(r"视频转gif", "", text, count=1).strip()
        params = self.proc._parse_video_args(text)

        source = self.media._get_video_source(event)
        url_in_text = re.search(r"https?://\S+", text)
        if not source and url_in_text:
            source = url_in_text.group(0)

        if not source:
            yield event.plain_result("❌ 请回复视频或附上视频链接")
            return

        yield event.plain_result(f"⏳ 视频转{self.cfg.output_format} 处理中...")
        tmp_path = None
        try:
            if isinstance(source, str) and source.startswith("http"):
                with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_file:
                    tmp_path = tmp_file.name
                from astrbot.core.utils.io import download_file as astrbot_download_file

                async def _dl():
                    await astrbot_download_file(source, tmp_path)
                    return tmp_path

                await self._run_task("download_video", _dl, timeout=min(self.cfg.task_timeout_sec, 180))
                video_path = tmp_path
            elif isinstance(source, str) and os.path.exists(source):
                video_path = source
            else:
                # try resolve file id
                resolved = await self.media._resolve_file_via_api(event, source)
                if not resolved:
                    yield event.plain_result("❌ 无法解析视频源")
                    return
                if resolved.startswith("http"):
                    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_file:
                        tmp_path = tmp_file.name
                    from astrbot.core.utils.io import download_file as astrbot_download_file

                    async def _dl2():
                        await astrbot_download_file(resolved, tmp_path)
                        return tmp_path

                    await self._run_task("download_video", _dl2, timeout=min(self.cfg.task_timeout_sec, 180))
                    video_path = tmp_path
                else:
                    video_path = resolved

            # duration guard
            if params.get("end") is not None:
                if params["end"] - params["start"] > self.cfg.max_gif_duration:
                    params["end"] = params["start"] + self.cfg.max_gif_duration
            else:
                params["end"] = params["start"] + self.cfg.max_gif_duration

            result_msg, gif_bytes = await self._run_cpu(
                "video_to_gif",
                self.proc._worker_video_to_gif_wrapper,
                video_path,
                params,
                timeout=self.cfg.task_timeout_sec,
            )
        except Exception as e:
            yield event.plain_result(f"❌ {e}")
            return
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        if gif_bytes:
            yield event.chain_result([Comp.Plain(result_msg), Comp.Image.fromBytes(gif_bytes.getvalue())])
        else:
            yield event.plain_result(result_msg)

    async def _handle_gif_task(self, event: AstrMessageEvent, algorithm_mode: int = 1):
        text = self._current_text(event)
        text, margins = self.crop._parse_margins(text)
        m = re.search(r"(\d+)\s*[xX×]\s*(\d+)", text)
        rows = cols = 0
        if m:
            rows, cols = int(m.group(1)), int(m.group(2))
        else:
            yield event.plain_result("❌ 请指定网格，如: 合成1gif 6x6 0.1")
            return

        duration = 0.1
        dur_m = re.search(r"(\d+(?:\.\d+)?)\s*s?", text)
        # find duration not part of grid
        for dm in re.finditer(r"(\d+(?:\.\d+)?)\s*s\b", text, re.I):
            duration = float(dm.group(1))
            break
        else:
            # bare float after grid
            after = text[m.end() :] if m else text
            dm2 = re.search(r"(\d+(?:\.\d+)?)", after)
            if dm2:
                val = float(dm2.group(1))
                if val < 10:  # likely seconds
                    duration = val

        yield event.plain_result(f"⏳ 合成GIF 算法{algorithm_mode} {rows}x{cols} ...")
        try:
            img_data = await self._run_task("download_image", lambda: self.media._resolve_image_bytes(event), timeout=60)
            if not img_data:
                yield event.plain_result("❌ 图片下载失败")
                return
            img_data, crop_msg = await self._run_cpu(
                "margin_crop", self.crop._crop_image_data, img_data, margins, timeout=30
            )
            func = self.proc.process_mode_1 if algorithm_mode == 1 else self.proc.process_mode_2
            res_msg, gif_bytes = await self._run_cpu(
                f"make_gif_mode{algorithm_mode}",
                func,
                img_data,
                rows,
                cols,
                duration,
                timeout=90,
            )
        except Exception as e:
            yield event.plain_result(f"❌ {e}")
            return

        if gif_bytes:
            yield event.chain_result(
                [Comp.Plain(res_msg + crop_msg), Comp.Image.fromBytes(gif_bytes.getvalue())]
            )
        else:
            yield event.plain_result(res_msg + crop_msg)

    @filter.command("合成gif")
    async def make_gif(self, event: AstrMessageEvent):
        text = self._current_text(event)
        mode = 2 if re.search(r"算法\s*2|mode\s*2", text, re.I) else 1
        async for r in self._handle_gif_task(event, mode):
            yield r

    @filter.command("合成1gif")
    async def make_gif_v1(self, event: AstrMessageEvent):
        async for r in self._handle_gif_task(event, 1):
            yield r

    @filter.command("合成2gif")
    async def make_gif_v2(self, event: AstrMessageEvent):
        async for r in self._handle_gif_task(event, 2):
            yield r

    async def _change_speed_impl(self, event: AstrMessageEvent, is_accelerate: bool, factor: float = 2.0):
        factor = max(0.1, min(float(factor), 20.0))
        ratio = (1.0 / factor) if is_accelerate else factor
        action = "加速" if is_accelerate else "减速"
        yield event.plain_result(f"⏳ GIF{action} {factor}x ...")
        try:
            img_data = await self._run_task("download_image", lambda: self.media._resolve_image_bytes(event), timeout=60)
            if not img_data:
                yield event.plain_result("❌ 图片下载失败")
                return
            res_msg, gif_bytes = await self._run_cpu(
                f"gif_{action}", self.proc.process_speed, img_data, ratio, timeout=60
            )
        except Exception as e:
            yield event.plain_result(f"❌ {e}")
            return
        if gif_bytes:
            yield event.chain_result(
                [Comp.Plain(res_msg), self._img_from_bytes(gif_bytes.getvalue(), "speed.gif")]
            )
        else:
            yield event.plain_result(res_msg)

    @filter.command("/g加速")
    async def accelerate_gif(self, event: AstrMessageEvent, factor: float = 2.0):
        async for r in self._change_speed_impl(event, True, factor):
            yield r

    @filter.command("/g减速")
    async def decelerate_gif(self, event: AstrMessageEvent, factor: float = 2.0):
        async for r in self._change_speed_impl(event, False, factor):
            yield r

    @filter.command("g加速")
    async def accelerate_gif_alias(self, event: AstrMessageEvent, factor: float = 2.0):
        """兼容无斜杠写法。"""
        async for r in self._change_speed_impl(event, True, factor):
            yield r

    @filter.command("g减速")
    async def decelerate_gif_alias(self, event: AstrMessageEvent, factor: float = 2.0):
        """兼容无斜杠写法。"""
        async for r in self._change_speed_impl(event, False, factor):
            yield r

    # 兼容旧指令提示
    @filter.command("加速")
    async def accelerate_gif_legacy(self, event: AstrMessageEvent):
        yield event.plain_result("⚠️ 指令已更名，请使用: /g加速 [倍数]\n示例: /g加速 1.5")

    @filter.command("减速")
    async def decelerate_gif_legacy(self, event: AstrMessageEvent):
        yield event.plain_result("⚠️ 指令已更名，请使用: /g减速 [倍数]\n示例: /g减速 2")

    @filter.command("gif分解")
    async def decompose_gif(self, event: AstrMessageEvent):
        yield event.plain_result("⏳ GIF分解中...")
        try:
            img_data = await self._run_task("download_image", lambda: self.media._resolve_image_bytes(event), timeout=60)
            if not img_data:
                yield event.plain_result("❌ 图片下载失败")
                return
            frames = await self._run_cpu("decompose", self.proc._worker_decompose, img_data, timeout=60)
        except Exception as e:
            yield event.plain_result(f"❌ {e}")
            return

        if isinstance(frames, str):
            yield event.plain_result(frames)
            return
        max_frames = min(len(frames), 20)
        nodes = [Comp.Node(name="GIF助手", content=[Comp.Plain(f"共{len(frames)}帧，显示前{max_frames}帧")])]
        for i, b in enumerate(frames[:max_frames]):
            nodes.append(
                Comp.Node(name="GIF助手", content=[Comp.Plain(f"第{i + 1}帧"), Comp.Image.fromBytes(b)])
            )
        yield event.chain_result([Comp.Nodes(nodes=nodes)])

    @filter.command("多图合成gif")
    async def multi_img_gif(self, event: AstrMessageEvent):
        text = self._current_text(event).replace("多图合成gif", "").strip()
        duration = 0.5
        m = re.search(r"(\d+(?:\.\d+)?)", text)
        if m:
            duration = max(0.05, min(float(m.group(1)), 10.0))

        yield event.plain_result("⏳ 收集多图...")
        try:
            img_urls = await self._run_task(
                "list_images", lambda: self.media._get_all_image_urls(event), timeout=30
            )
            if not img_urls:
                yield event.plain_result("❌ 未检测到多张图片")
                return
            yield event.plain_result(f"⏳ 合成 {len(img_urls)} 张 -> GIF ...")

            async def _download_all():
                import asyncio

                tasks = [self.media._download_content(url) for url in img_urls]
                return await asyncio.gather(*tasks)

            results = await self._run_task("download_multi", _download_all, timeout=120)
            valid = [b for b in results if b]
            if not valid:
                yield event.plain_result("❌ 图片下载全部失败")
                return
            res_msg, gif_io = await self._run_cpu(
                "multi_gif", self.proc._worker_multi_image_gif, valid, duration, timeout=90
            )
        except Exception as e:
            yield event.plain_result(f"❌ {e}")
            return

        if gif_io:
            yield event.chain_result(
                [
                    Comp.Plain(f"{res_msg}\n画布适应最大尺寸，自动居中填充"),
                    Comp.Image.fromBytes(gif_io.getvalue()),
                ]
            )
        else:
            yield event.plain_result(res_msg)

    @filter.command("表情包做旧")
    async def age_meme(self, event: AstrMessageEvent):
        text = self._current_text(event).replace("表情包做旧", "").strip()
        times = 10
        m = re.search(r"(\d+)", text)
        if m:
            times = max(1, min(int(m.group(1)), 50))
        if times <= 5:
            level = "轻度做旧"
        elif times <= 15:
            level = "中度包浆"
        elif times <= 30:
            level = "重度老化"
        else:
            level = "极限做旧 (赛博遗产级别)"

        yield event.plain_result(f"⏳ 做旧中... ({times}次, {level})")
        try:
            img_data = await self._run_task("download_image", lambda: self.media._resolve_image_bytes(event), timeout=60)
            if not img_data:
                yield event.plain_result("❌ 图片下载失败")
                return
            res_msg, result_bytes = await self._run_cpu(
                "age_meme", self.proc._worker_age_meme, img_data, times, timeout=120
            )
        except Exception as e:
            yield event.plain_result(f"❌ {e}")
            return

        if result_bytes:
            yield event.chain_result(
                [Comp.Plain(f"{res_msg}\n💡 {level}"), Comp.Image.fromBytes(result_bytes)]
            )
        else:
            yield event.plain_result(res_msg)

    @filter.command("批量去白边")
    async def batch_remove_white_border(self, event: AstrMessageEvent):
        msg_text = event.message_str.replace("批量去白边", "").strip()
        threshold = 240
        num_match = re.search(r"(\d+)", msg_text)
        if num_match:
            threshold = max(150, min(int(num_match.group(1)), 250))

        yield event.plain_result(f"⏳ 收集图片... (阈值:{threshold})")
        try:
            img_urls = await self._run_task(
                "list_images", lambda: self.media._get_all_image_urls(event), timeout=30
            )
            if not img_urls:
                yield event.plain_result("❌ 未检测到图片")
                return
            yield event.plain_result(f"⏳ 批量处理 {len(img_urls)} 张...")

            async def _process_all():
                import asyncio

                async def process_one(url):
                    img_data = await self.media._download_content(url)
                    if not img_data:
                        return None
                    result, _info = await asyncio.to_thread(
                        self.crop._worker_auto_crop, img_data, threshold
                    )
                    return result if result else None

                return await asyncio.gather(*[process_one(u) for u in img_urls])

            results = await self._run_task("batch_border", _process_all, timeout=self.cfg.task_timeout_sec)
            valid_results = [r for r in results if r is not None]
        except Exception as e:
            yield event.plain_result(f"❌ {e}")
            return

        if not valid_results:
            yield event.plain_result("❌ 所有图片处理失败")
            return

        nodes = [
            Comp.Node(
                name="批量去白边",
                content=[
                    Comp.Plain(
                        f"✅ 处理完成\n共 {len(img_urls)} 张，成功 {len(valid_results)} 张\n阈值: {threshold}"
                    )
                ],
            )
        ]
        for i, img_bytes in enumerate(valid_results[:30]):
            nodes.append(
                Comp.Node(
                    name="批量去白边",
                    content=[Comp.Plain(f"第{i+1}张"), Comp.Image.fromBytes(img_bytes)],
                )
            )
        if len(valid_results) > 30:
            nodes.append(
                Comp.Node(
                    name="批量去白边",
                    content=[Comp.Plain(f"⚠️ 共{len(valid_results)}张，仅显示前30张")],
                )
            )
        yield event.chain_result([Comp.Nodes(nodes=nodes)])

