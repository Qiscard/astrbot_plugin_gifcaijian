# -*- coding: utf-8 -*-
"""Image/GIF/video processors."""
from __future__ import annotations

import io
import re
from typing import List, Optional, Tuple

from PIL import Image as PILImage, ImageSequence, ImageFilter, ImageOps, ImageEnhance
from astrbot.api import logger

from .animation import save_animation
from .config_helpers import Cfg
from .deps import imageio, np, has_imageio


class Processors:
    def __init__(self, cfg: Cfg):
        self.cfg = cfg

    def _parse_video_args(self, text: str):
        default_scale = self.cfg.get('default_scale', 0.3)
        default_fps = self.cfg.get('default_fps', 10)
        params = {
            'start': 0.0, 'end': None, 'fps': default_fps,
            'step': 1, 'scale': default_scale, 'force_step': False
        }
        time_range = re.search(r'(\d+(?:\.\d+)?)[sS]?\s*[-~]\s*(\d+(?:\.\d+)?)[sS]?', text)
        if time_range:
            params['start'] = float(time_range.group(1))
            params['end'] = float(time_range.group(2))
            text = text.replace(time_range.group(0), " ")
        else:
            start_match = re.search(r'(?:开始|start)\s*(\d+(?:\.\d+)?)', text)
            dur_match = re.search(r'(?:时长|len|time)\s*(\d+(?:\.\d+)?)', text)
            if start_match: 
                params['start'] = float(start_match.group(1))
            if dur_match: 
                params['end'] = params['start'] + float(dur_match.group(1))

        step_match = re.search(r'(\d+)\s*/\s*(\d+)', text)
        if step_match:
            n1 = int(step_match.group(1))
            n2 = int(step_match.group(2))
            step_val = max(n1, n2)
            if step_val > 0:
                params['step'] = step_val
                params['fps'] = None
                params['force_step'] = True
            text = text.replace(step_match.group(0), " ")
        else:
            fps_match = re.search(r'(?:fps|帧率)\s*(\d+)', text)
            if fps_match: 
                params['fps'] = int(fps_match.group(1))

        scale_match = re.search(r'\b(0\.\d+|1\.0)\b', text)
        if scale_match: 
            params['scale'] = float(scale_match.group(1))
        if params['scale'] < 0.1: 
            params['scale'] = 0.1
        if params['scale'] > 1.0: 
            params['scale'] = 1.0
        return params

    # --- 核心处理逻辑 (视频转GIF) ---
    def _process_gif_core(self, video_path: str, params: dict, max_colors: int = 256):
        try:
            reader = imageio.get_reader(video_path, format='FFMPEG')
            meta = reader.get_meta_data()
            video_duration = meta.get('duration', 100)
            src_fps = meta.get('fps', 30) or 30
            start_t = params['start']
            end_t = params['end'] if params['end'] is not None else video_duration
            max_dur_conf = self.cfg.get('max_gif_duration', 10.0)
            warn_msg = ""
            if (end_t - start_t) > max_dur_conf:
                end_t = start_t + max_dur_conf
                warn_msg = f"(限时{max_dur_conf}s)"
            end_t = min(end_t, video_duration)
            if start_t >= video_duration: 
                return None, f"❌ 开始时间超限", 0

            step = 1
            target_fps = 0
            if params.get('force_step'):
                step = params['step']
                target_fps = src_fps / step
            elif params.get('fps'):
                target_fps = params['fps']
                if target_fps > src_fps: 
                    target_fps = src_fps
                step = max(1, int(src_fps / target_fps))
            else:
                step = 3
                target_fps = src_fps / step

            frames = []
            output_fmt = self.cfg.get('output_format', 'GIF').upper()
            for i, frame in enumerate(reader):
                current_time = i / src_fps
                if current_time < start_t: 
                    continue
                if current_time > end_t: 
                    break
                if i % step == 0:
                    pil_img = PILImage.fromarray(frame)
                    w, h = pil_img.size
                    new_w = int(w * params['scale'])
                    new_h = int(h * params['scale'])
                    pil_img = pil_img.resize((new_w, new_h), PILImage.Resampling.BILINEAR)
                    if output_fmt == 'GIF' and max_colors < 256:
                        pil_img = pil_img.quantize(colors=max_colors, method=1, dither=PILImage.Dither.FLOYDSTEINBERG)
                    frames.append(pil_img)
                if len(frames) > 400:
                    warn_msg += " [帧数截断]"
                    break
            reader.close()
            if not frames: 
                return None, "❌ 无有效帧", 0
            output = io.BytesIO()
            duration_ms = int(1000 / target_fps) if target_fps > 0 else 100
            save_animation(self.cfg, output, frames, duration_ms, loop=0)
            output.seek(0)
            size_mb = output.getbuffer().nbytes / 1024 / 1024
            info = f"时间:{start_t}-{end_t:.1f}s {warn_msg}\n格式:{output_fmt} | FPS:{target_fps:.1f}\n缩放:{params['scale']} | 体积:{size_mb:.2f}MB"
            return output, info, size_mb
        except Exception as e:
            return None, f"内部错误: {repr(e)}", 0

    def _worker_video_to_gif_wrapper(self, video_path: str, params: dict):
        if imageio is None: 
            return "❌ 缺少依赖库 imageio", None
        max_colors = self.cfg.get('gif_max_colors', 256)
        gif_io, msg, size_mb = self._process_gif_core(video_path, params, max_colors)
        if not gif_io: 
            return msg, None
        output_fmt = self.cfg.get('output_format', 'GIF').upper()
        if size_mb > 10.0 and output_fmt == 'GIF':
            new_params = params.copy()
            new_msg_prefix = f"⚠️ 初次体积{size_mb:.1f}MB过大，自动压缩中...\n"
            new_colors = 128 if max_colors > 128 else 64
            new_params['scale'] = round(params['scale'] * 0.8, 2)
            if new_params['scale'] < 0.1: 
                new_params['scale'] = 0.1
            retry_io, retry_msg, retry_size = self._process_gif_core(video_path, new_params, new_colors)
            if retry_io and retry_size < size_mb:
                return new_msg_prefix + retry_msg, retry_io
            else:
                return f"⚠️ 压缩失败({retry_size:.1f}MB)，原版:\n" + msg, gif_io
        return "✅ 转换成功\n" + msg, gif_io

    def _worker_local_line_art(self, img_bytes: bytes) -> bytes:
        try:
            img = PILImage.open(io.BytesIO(img_bytes)).convert("RGB")
            gray = img.convert("L")
            edges = gray.filter(ImageFilter.FIND_EDGES)
            result = ImageOps.invert(edges)
            enhancer = ImageEnhance.Contrast(result)
            result = enhancer.enhance(3.0)
            output = io.BytesIO()
            result.save(output, format='JPEG', quality=90)
            return output.getvalue()
        except Exception as e:
            logger.error(f"线稿转换失败: {e}")
            return None

    def process_mode_1(self, img_data: bytes, rows: int, cols: int, duration_sec: float):
        try:
            img = PILImage.open(io.BytesIO(img_data))
            if getattr(img, "is_animated", False): 
                img.seek(0)
            img = img.convert("RGBA")
            w, h = img.size
            cw, ch = w // cols, h // rows
            if cw < 2 or ch < 2: 
                return f"⚠️ 单格太小 ({cw}x{ch})", None
            frames = []
            for r in range(rows):
                for c in range(cols):
                    frames.append(img.crop((c * cw, r * ch, (c + 1) * cw, (r + 1) * ch)))
            output = io.BytesIO()
            save_animation(self.cfg, output, frames, int(duration_sec * 1000), loop=0)
            output.seek(0)
            return f"✅ 合成成功\n算法1 | {w}x{h} | {rows}行{cols}列", output
        except Exception as e:
            return f"逻辑异常: {e}", None

    def process_mode_2(self, img_data: bytes, rows: int, cols: int, duration_sec: float):
        try:
            img = PILImage.open(io.BytesIO(img_data))
            if getattr(img, "is_animated", False):
                img.seek(0)
            img = img.convert("RGBA")
            # 透明度二值化：alpha<128 视为透明，其余不透明 —— 用 numpy 向量化
            if np is not None:
                arr = np.asarray(img).copy()
                transparent_mask = arr[:, :, 3] < 128
                arr[transparent_mask, :4] = (0, 0, 0, 0)
                arr[~transparent_mask, 3] = 255
                img = PILImage.fromarray(arr, "RGBA")
            else:
                datas = img.getdata()
                new_data = [(0, 0, 0, 0) if item[3] < 128 else (item[0], item[1], item[2], 255) for item in datas]
                img.putdata(new_data)
            has_trans = bool(np.any(np.asarray(img)[:, :, 3] == 0)) if np is not None else bool(img.getchannel("A").getextrema()[0] == 0)
            master_pal = img.convert("RGB").quantize(colors=255 if has_trans else 256, method=1)
            w, h = img.size
            cw, ch = w // cols, h // rows
            if cw < 2 or ch < 2: 
                return f"⚠️ 单格太小 ({cw}x{ch})", None
            frames = []
            for r in range(rows):
                for c in range(cols):
                    crop = img.crop((c * cw, r * ch, (c + 1) * cw, (r + 1) * ch))
                    frame = crop.convert("RGB").quantize(palette=master_pal)
                    if has_trans:
                        mask = crop.split()[3].point(lambda a: 255 if a < 128 else 0)
                        frame.paste(255, mask=mask)
                    frames.append(frame)
            output = io.BytesIO()
            fmt = self.cfg.get('output_format', 'GIF').upper()
            if fmt == 'GIF':
                frames[0].save(output, format='GIF', save_all=True, append_images=frames[1:],
                               duration=int(duration_sec * 1000), loop=0, disposal=2,
                               transparency=255 if has_trans else None, optimize=True)
            else:
                save_animation(self.cfg, output, frames, int(duration_sec * 1000), loop=0)
            output.seek(0)
            return f"✅ 合成成功\n算法2 | {w}x{h} | {rows}行{cols}列", output
        except Exception as e:
            return f"逻辑异常: {e}", None


    def process_speed(self, img_data: bytes, ratio: float):
        """按 ratio 缩放帧时长（ratio<1 加速，>1 减速）。

        修复多次变速后帧率异常的根因：
        1) 旧逻辑 max(10, int(dur*ratio)) 会把加速结果钉死在 10ms，再加速无效、节奏失真；
        2) palette 帧浅拷贝 + optimize 重编码易丢 disposal/时长；
        3) GIF 标准延迟粒度为 10ms（1/100s），亚 10ms 目标通过均匀抽帧表达，避免假帧率。
        """
        try:
            img = PILImage.open(io.BytesIO(img_data))
            if not getattr(img, "is_animated", False):
                return "这不是GIF", None

            ratio = float(ratio)
            if ratio <= 0:
                return "❌ 变速倍率无效", None

            default_dur = int(img.info.get("duration", 100) or 100)
            if default_dur <= 0:
                default_dur = 100

            src_frames: List[PILImage.Image] = []
            raw_durs: List[int] = []
            loop = img.info.get("loop", 0)
            try:
                loop = int(loop)
            except (TypeError, ValueError):
                loop = 0

            for frame in ImageSequence.Iterator(img):
                raw = frame.info.get("duration", default_dur)
                try:
                    raw = int(raw)
                except (TypeError, ValueError):
                    raw = default_dur
                if raw <= 0:
                    raw = default_dur
                # GIF 读回通常已是 10ms 倍数；规整到 >=10ms 的 10ms 网格，保证多轮可逆
                raw = max(10, int(round(raw / 10.0)) * 10)
                raw_durs.append(raw)
                src_frames.append(frame.convert("RGBA").copy())

            if not src_frames:
                return "❌ 无法读取GIF帧", None

            targets = [d * ratio for d in raw_durs]
            out_frames: List[PILImage.Image] = []
            out_durs: List[int] = []

            def _snap_ms(ms: float) -> int:
                """落到 GIF 可用的 10ms 网格，范围 10ms~60s。"""
                v = int(round(float(ms) / 10.0)) * 10
                return max(10, min(v, 60000))

            if ratio < 1.0:
                # 加速：目标间隔 <10ms 时均匀合并/抽帧，用更少帧 + >=10ms 间隔表达更快节奏
                i = 0
                n = len(src_frames)
                while i < n:
                    acc = targets[i]
                    last = i
                    i += 1
                    while i < n and acc < 10.0:
                        acc += targets[i]
                        last = i
                        i += 1
                    out_frames.append(src_frames[last])
                    out_durs.append(_snap_ms(acc))
            else:
                # 减速或 1x：只拉长间隔，不插帧
                for fr, td in zip(src_frames, targets):
                    out_frames.append(fr)
                    out_durs.append(_snap_ms(td))

            if not out_frames:
                return "❌ 变速后无有效帧", None

            output = io.BytesIO()
            # optimize=False，避免 Pillow 合并帧导致 duration 列表错位
            out_frames[0].save(
                output,
                format="GIF",
                save_all=True,
                append_images=out_frames[1:],
                duration=out_durs,
                loop=loop,
                disposal=2,
                optimize=False,
            )
            output.seek(0)

            avg_in = sum(raw_durs) / len(raw_durs)
            avg_out = sum(out_durs) / len(out_durs)
            total_in_s = sum(raw_durs) / 1000.0
            total_out_s = sum(out_durs) / 1000.0
            fps_in = 1000.0 / avg_in if avg_in > 0 else 0.0
            fps_out = 1000.0 / avg_out if avg_out > 0 else 0.0
            drop_note = ""
            if len(out_frames) != len(src_frames):
                drop_note = f"\n抽帧: {len(src_frames)} → {len(out_frames)}（GIF最小间隔10ms）"
            msg = (
                f"✅ 变速完成\n"
                f"帧数: {len(src_frames)} → {len(out_frames)} | "
                f"总时长: {total_in_s:.2f}s → {total_out_s:.2f}s\n"
                f"平均帧间隔: {avg_in:.0f}ms → {avg_out:.0f}ms\n"
                f"等效FPS: {fps_in:.2f} → {fps_out:.2f}"
                f"{drop_note}"
            )
            return msg, output
        except Exception as e:
            return f"异常: {e}", None


    def _worker_decompose(self, img_data: bytes):
        try:
            img = PILImage.open(io.BytesIO(img_data))
            if not getattr(img, "is_animated", False): 
                return "⚠️ 不是GIF动画"
            frames = []
            for i, frame in enumerate(ImageSequence.Iterator(img)):
                if i >= 100: 
                    break
                out = io.BytesIO()
                frame.copy().convert("RGBA").save(out, format='PNG')
                frames.append(out.getvalue())
            return frames
        except Exception as e:
            return f"❌ 出错: {e}"

    # --- 多图合成GIF ---
    def _worker_multi_image_gif(self, images_bytes: list[bytes], duration_sec: float):
        try:
            pil_images = []
            max_w, max_h = 0, 0

            for b in images_bytes:
                try:
                    img = PILImage.open(io.BytesIO(b)).convert("RGBA")
                    if getattr(img, "is_animated", False):
                        img.seek(0)
                        img = img.copy()
                    pil_images.append(img)
                    max_w = max(max_w, img.width)
                    max_h = max(max_h, img.height)
                    
                    # 限制最多50张
                    if len(pil_images) >= 50:
                        break
                except Exception as e:
                    logger.warning(f"加载图片失败: {e}")

            if not pil_images:
                return "❌ 没有有效的图片", None

            frames = []
            for img in pil_images:
                bg = PILImage.new("RGBA", (max_w, max_h), (255, 255, 255, 0))
                src_ratio = img.width / img.height
                tgt_ratio = max_w / max_h

                if src_ratio > tgt_ratio:
                    new_w = max_w
                    new_h = int(max_w / src_ratio)
                else:
                    new_h = max_h
                    new_w = int(max_h * src_ratio)

                img_resized = img.resize((new_w, new_h), PILImage.Resampling.BILINEAR)
                paste_x = (max_w - new_w) // 2
                paste_y = (max_h - new_h) // 2
                bg.paste(img_resized, (paste_x, paste_y), mask=img_resized if 'A' in img_resized.getbands() else None)
                frames.append(bg)

            output = io.BytesIO()
            duration_ms = int(duration_sec * 1000)
            save_animation(self.cfg, output, frames, duration_ms, loop=0)
            output.seek(0)

            return f"✅ 合成成功 ({len(frames)}张)", output

        except Exception as e:
            return f"合成出错: {repr(e)}", None

    def _age_single_frame(self, img: PILImage.Image, times: int) -> PILImage.Image:
        """对单帧图片进行做旧处理（通道偏移已用 numpy 向量化）"""
        import random

        if img.mode != "RGB":
            img = img.convert("RGB")

        for i in range(times):
            # 绿色通道偏移（每3次）—— 用 numpy 向量化代替逐像素 point
            if i % 3 == 0 and np is not None:
                arr = np.asarray(img).astype(np.int16)
                green_boost = random.randint(1, 2)
                red_reduce = random.randint(0, 1)
                blue_reduce = random.randint(0, 1)
                if red_reduce > 0:
                    arr[:, :, 0] = np.clip(arr[:, :, 0] - red_reduce, 0, 255)
                arr[:, :, 1] = np.clip(arr[:, :, 1] + green_boost, 0, 255)
                if blue_reduce > 0:
                    arr[:, :, 2] = np.clip(arr[:, :, 2] - blue_reduce, 0, 255)
                img = PILImage.fromarray(arr.astype(np.uint8), "RGB")
            
            # JPEG压缩失真
            quality = max(25, 70 - i * 3)
            temp_io = io.BytesIO()
            img.save(temp_io, format='JPEG', quality=quality)
            temp_io.seek(0)
            img = PILImage.open(temp_io).convert("RGB")
            
            # 轻微模糊（每3次）
            if i % 3 == 0:
                blur_radius = 0.2 + (i // 3) * 0.1
                img = img.filter(ImageFilter.GaussianBlur(radius=blur_radius))
            
            # 轻微锐化（偶尔）
            if i % 5 == 2:
                img = img.filter(ImageFilter.SHARPEN)
            
            # 降低饱和度（每2次）
            if i % 2 == 0:
                enhancer = ImageEnhance.Color(img)
                saturation = max(0.85, 1.0 - 0.015)
                img = enhancer.enhance(saturation)
            
            # 降低对比度（每2次）
            if i % 2 == 1:
                enhancer = ImageEnhance.Contrast(img)
                contrast = max(0.85, 1.0 - 0.01)
                img = enhancer.enhance(contrast)
        
        return img

    def _worker_age_meme(self, img_data: bytes, times: int) -> tuple[str, bytes]:
        try:
            img = PILImage.open(io.BytesIO(img_data))
            is_animated = getattr(img, "is_animated", False)
            
            if is_animated:
                frames = []
                durations = []
                
                for frame in ImageSequence.Iterator(img):
                    dur = frame.info.get('duration', 100)
                    if dur <= 0:
                        dur = 100
                    durations.append(dur)
                    frame_copy = frame.copy().convert("RGB")
                    aged_frame = self._age_single_frame(frame_copy, times)
                    p_frame = aged_frame.convert("P", palette=PILImage.Palette.ADAPTIVE, colors=256)
                    frames.append(p_frame)
                
                if not frames:
                    return "❌ 无法读取动图帧", None
                
                output = io.BytesIO()
                frames[0].save(
                    output, 
                    format='GIF', 
                    save_all=True, 
                    append_images=frames[1:],
                    duration=durations, 
                    loop=0, 
                    disposal=2, 
                    optimize=False
                )
                output.seek(0)
                return f"✅ 做旧成功 (动图 {len(frames)}帧, {times}次传播)", output.getvalue()
            else:
                img = img.convert("RGB")
                aged_img = self._age_single_frame(img, times)
                output = io.BytesIO()
                final_quality = max(30, 70 - times * 3)
                aged_img.save(output, format='JPEG', quality=final_quality)
                return f"✅ 做旧成功 ({times}次传播, 质量{final_quality}%)", output.getvalue()
                
        except Exception as e:
            import traceback
            return f"❌ 处理失败: {repr(e)}", None
