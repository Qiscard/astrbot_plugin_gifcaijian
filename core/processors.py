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
from .safety import (
    SafetyError,
    check_file_path,
    check_raw_bytes,
    compress_image,
    open_image_checked,
    sniff_kind_from_bytes,
)


class Processors:
    def __init__(self, cfg: Cfg):
        self.cfg = cfg

    def _open_checked(self, img_data: bytes):
        """Open with size/pixel/GIF frame prechecks."""
        return open_image_checked(img_data, self.cfg)

    def _compress_frame(self, image):
        img, _ = compress_image(image, self.cfg)
        return img


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
            max_out_frames = self.cfg.max_gif_frames
            for i, frame in enumerate(reader):
                current_time = i / src_fps
                if current_time < start_t: 
                    continue
                if current_time > end_t: 
                    break
                if i % step == 0:
                    if len(frames) >= max_out_frames:
                        break
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
        try:
            check_file_path(video_path, self.cfg, kind="video")
        except SafetyError as e:
            return f"❌ {e}", None
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
            img = self._open_checked(img_bytes)
            img = self._compress_frame(img).convert("RGB")
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
            img = self._open_checked(img_data)
            if getattr(img, "is_animated", False): 
                img.seek(0)
            img = self._compress_frame(img.convert("RGBA"))
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
            img = self._open_checked(img_data)
            if getattr(img, "is_animated", False):
                img.seek(0)
            img = self._compress_frame(img.convert("RGBA"))
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


    def process_speed(self, img_data: bytes, ratio: float, min_frame_ms: int = 20):
        """按 ratio 改变播放速度（ratio<1 加速，ratio>1 减速）。

        QQ 客户端已知问题：
        - GCE delay=1~2（10~20ms）常被当成约 100ms，造成「预览极慢/卡顿」；
        - 在线预览与下载后本地播放器表现不一致；
        - 末帧塞超长 duration 会在 QQ 里拖成慢动作。

        兼容策略：
        - 输出每帧 delay >= 20ms（可配），且为 10ms 整数倍；
        - 以总时长 * ratio 为目标，加速优先均匀抽帧，减速只拉长间隔；
        - 时长在各帧间均匀分摊，禁止把残余全堆到末帧；
        - 合成全帧后保留透明度（透明索引 255），避免透明 GIF 被糊成黑底。
        """
        try:
            img = self._open_checked(img_data)
            if not getattr(img, "is_animated", False):
                return "这不是GIF", None

            ratio = float(ratio)
            if ratio <= 0:
                return "❌ 变速倍率无效", None

            try:
                MIN_MS = int(min_frame_ms)
            except (TypeError, ValueError):
                MIN_MS = 20
            MIN_MS = max(10, min(MIN_MS, 1000))
            MIN_MS = int(round(MIN_MS / 10.0)) * 10
            if MIN_MS < 10:
                MIN_MS = 10
            GRID = 10
            TRANSPARENT_INDEX = 255

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

            # 逐帧合成，避免 partial-frame GIF 抽到透明碎片；始终保留 RGBA
            canvas = PILImage.new("RGBA", img.size, (0, 0, 0, 0))
            has_alpha = False
            max_frames = self.cfg.max_gif_frames
            for fi, frame in enumerate(ImageSequence.Iterator(img)):
                if fi >= max_frames:
                    break
                raw = frame.info.get("duration", default_dur)
                try:
                    raw = int(raw)
                except (TypeError, ValueError):
                    raw = default_dur
                if raw <= 0:
                    raw = default_dur
                raw = max(GRID, int(round(raw / GRID)) * GRID)
                raw_durs.append(raw)

                fr = frame.convert("RGBA")
                if fr.size != canvas.size:
                    layer = PILImage.new("RGBA", canvas.size, (0, 0, 0, 0))
                    layer.paste(fr, (0, 0), fr)
                    fr = layer
                disposal = frame.info.get("disposal", 2)
                prev = canvas.copy()
                canvas.paste(fr, (0, 0), fr)
                composed = self._compress_frame(canvas.copy())
                if composed.getchannel("A").getextrema()[0] < 255:
                    has_alpha = True
                src_frames.append(composed)
                if disposal == 2:
                    canvas = PILImage.new("RGBA", img.size, (0, 0, 0, 0))
                elif disposal == 3:
                    canvas = prev

            n = len(src_frames)
            if n == 0:
                return "❌ 无法读取GIF帧", None

            total_in = float(sum(raw_durs))
            target_total = max(float(MIN_MS), total_in * ratio)

            def _even_durs(count: int, total_ms: float) -> List[int]:
                count = max(1, count)
                min_total = count * MIN_MS
                total_i = int(round(float(total_ms) / GRID)) * GRID
                total_i = max(min_total, total_i)
                base = total_i // count
                base = max(MIN_MS, int(base // GRID) * GRID)
                durs = [base] * count
                rem = (total_i - sum(durs)) // GRID
                i = 0
                guard = 0
                while rem != 0 and guard < count * 200:
                    guard += 1
                    idx = i % count
                    if rem > 0:
                        durs[idx] += GRID
                        rem -= 1
                    else:
                        if durs[idx] - GRID >= MIN_MS:
                            durs[idx] -= GRID
                            rem += 1
                    i += 1
                return durs

            def _sample_indices(src_n: int, out_n: int) -> List[int]:
                if out_n <= 1:
                    return [0]
                if out_n >= src_n:
                    return list(range(src_n))
                raw_idx = [
                    int(round(i * (src_n - 1) / (out_n - 1)))
                    for i in range(out_n)
                ]
                out: List[int] = []
                seen = set()
                for idx in raw_idx:
                    idx = max(0, min(src_n - 1, idx))
                    if idx not in seen:
                        out.append(idx)
                        seen.add(idx)
                if len(out) < out_n:
                    for idx in range(src_n):
                        if idx not in seen:
                            out.append(idx)
                            seen.add(idx)
                            if len(out) >= out_n:
                                break
                    out.sort()
                return out[:out_n]

            if ratio < 1.0:
                simple = [
                    max(MIN_MS, int(round(d * ratio / GRID)) * GRID)
                    for d in raw_durs
                ]
                simple_total = float(sum(simple))
                if simple_total <= target_total * 1.12:
                    indices = list(range(n))
                    out_durs = _even_durs(n, target_total)
                else:
                    out_n = max(1, int(round(target_total / MIN_MS)))
                    out_n = min(n, max(1, out_n))
                    indices = _sample_indices(n, out_n)
                    out_durs = _even_durs(len(indices), target_total)
            else:
                indices = list(range(n))
                out_durs = _even_durs(n, target_total)

            out_rgba = [src_frames[i] for i in indices]

            def _rgba_to_gif_frame(image: PILImage.Image, colors: int = 255) -> PILImage.Image:
                if image.mode != "RGBA":
                    image = image.convert("RGBA")
                alpha = image.getchannel("A")
                frame_has_alpha = alpha.getextrema()[0] < 255
                # 有透明时预留索引 255
                quant_colors = min(colors, 255) if has_alpha else min(colors, 256)
                p_frame = image.convert("RGB").quantize(colors=quant_colors, method=1)
                if not has_alpha:
                    return p_frame
                palette = p_frame.getpalette() or []
                if len(palette) < 768:
                    palette.extend([0] * (768 - len(palette)))
                palette[TRANSPARENT_INDEX * 3: TRANSPARENT_INDEX * 3 + 3] = [0, 0, 0]
                p_frame.putpalette(palette)
                if frame_has_alpha:
                    mask = alpha.point(lambda a: 255 if a < 128 else 0)
                    p_frame.paste(TRANSPARENT_INDEX, mask=mask)
                p_frame.info["transparency"] = TRANSPARENT_INDEX
                return p_frame

            gif_frames = [_rgba_to_gif_frame(f) for f in out_rgba]
            output = io.BytesIO()
            save_kwargs = {
                "format": "GIF",
                "save_all": True,
                "append_images": gif_frames[1:],
                "duration": out_durs,
                "loop": 0 if loop is None else loop,
                "disposal": 2,
                "optimize": False,
            }
            if has_alpha:
                save_kwargs["transparency"] = TRANSPARENT_INDEX
            gif_frames[0].save(output, **save_kwargs)
            output.seek(0)

            total_out = float(sum(out_durs))
            out_n = len(out_durs)
            avg_in = total_in / n
            avg_out = total_out / out_n
            fps_in = 1000.0 / avg_in if avg_in > 0 else 0.0
            fps_out = 1000.0 / avg_out if avg_out > 0 else 0.0
            speed_x = total_in / total_out if total_out > 0 else 0.0
            note = ""
            if out_n != n:
                note = f"\n抽帧: {n} → {out_n}（最小间隔{MIN_MS}ms）"
            alpha_note = " | 保留透明" if has_alpha else ""
            msg = (
                f"✅ 变速完成\n"
                f"帧数: {n} → {out_n} | "
                f"总时长: {total_in/1000.0:.2f}s → {total_out/1000.0:.2f}s\n"
                f"平均帧间隔: {avg_in:.0f}ms → {avg_out:.0f}ms\n"
                f"等效FPS: {fps_in:.2f} → {fps_out:.2f} | 实际约 {speed_x:.2f}x | 最小间隔{MIN_MS}ms"
                f"{alpha_note}{note}"
            )
            return msg, output
        except Exception as e:
            return f"异常: {e}", None


    def _worker_decompose(self, img_data: bytes):
        try:
            img = self._open_checked(img_data)
            if not getattr(img, "is_animated", False): 
                return "⚠️ 不是GIF动画"
            frames = []
            max_frames = self.cfg.max_gif_frames
            for i, frame in enumerate(ImageSequence.Iterator(img)):
                if i >= max_frames:
                    break
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
                    kind = sniff_kind_from_bytes(b)
                    check_raw_bytes(b, self.cfg, kind="gif" if kind == "gif" else "image")
                    img = self._compress_frame(self._open_checked(b).convert("RGBA"))
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
            img = self._open_checked(img_data)
            is_animated = getattr(img, "is_animated", False)

            if is_animated:
                frames = []
                durations = []
                max_frames = self.cfg.max_gif_frames
                for fi, frame in enumerate(ImageSequence.Iterator(img)):
                    if fi >= max_frames:
                        break
                    dur = frame.info.get("duration", 100)
                    try:
                        dur = int(dur)
                    except (TypeError, ValueError):
                        dur = 100
                    if dur <= 0:
                        dur = 100
                    durations.append(dur)
                    frame_copy = self._compress_frame(frame.copy().convert("RGB"))
                    aged_frame = self._age_single_frame(frame_copy, times)
                    p_frame = aged_frame.convert("P", palette=PILImage.Palette.ADAPTIVE, colors=256)
                    frames.append(p_frame)

                if not frames:
                    return "❌ 无法读取动图帧", None

                output = io.BytesIO()
                frames[0].save(
                    output,
                    format="GIF",
                    save_all=True,
                    append_images=frames[1:],
                    duration=durations,
                    loop=0,
                    disposal=2,
                    optimize=False,
                )
                output.seek(0)
                return f"✅ 做旧成功 (动图 {len(frames)}帧, {times}次传播)", output.getvalue()

            img = self._compress_frame(img.convert("RGB"))
            aged_img = self._age_single_frame(img, times)
            output = io.BytesIO()
            final_quality = max(30, 70 - times * 3)
            aged_img.save(output, format="JPEG", quality=final_quality)
            return f"✅ 做旧成功 ({times}次传播, 质量{final_quality}%)", output.getvalue()

        except Exception as e:
            return f"❌ 处理失败: {repr(e)}", None


    # ------------------------------------------------------------------
    # 镜像 / 反色（移植自 astrbot_plugin_pic_mirror 思路）
    # ------------------------------------------------------------------
    GIF_TRANSPARENT_INDEX = 255

    @staticmethod
    def _apply_invert(image: PILImage.Image) -> PILImage.Image:
        """反转颜色，保留 alpha。"""
        if image.mode == "RGBA":
            r, g, b, a = image.split()
            inv = ImageOps.invert(PILImage.merge("RGB", (r, g, b)))
            inv.putalpha(a)
            return inv
        if image.mode == "LA":
            lum, a = image.split()
            return PILImage.merge("LA", (ImageOps.invert(lum), a))
        if image.mode == "L":
            return ImageOps.invert(image)
        if image.mode != "RGB":
            image = image.convert("RGB")
        return ImageOps.invert(image)

    @staticmethod
    def _apply_mirror(image: PILImage.Image, mode: str) -> PILImage.Image:
        """
        mode:
          left_to_right / right_to_left / top_to_bottom / bottom_to_top / invert
        """
        if mode == "invert":
            return Processors._apply_invert(image)

        width, height = image.size
        result = PILImage.new(image.mode, (width, height))

        if mode == "left_to_right":
            half_width = (width + 1) // 2
            other_half = width - half_width
            left_half = image.crop((0, 0, half_width, height))
            right_half = left_half.transpose(PILImage.FLIP_LEFT_RIGHT)
            result.paste(left_half, (0, 0))
            if other_half > 0:
                right_piece = right_half.crop(
                    (right_half.width - other_half, 0, right_half.width, height)
                )
                # 保留 alpha 时用自身作 mask
                if right_piece.mode in ("RGBA", "LA"):
                    result.paste(right_piece, (half_width, 0), right_piece)
                else:
                    result.paste(right_piece, (half_width, 0))
        elif mode == "right_to_left":
            half_width = (width + 1) // 2
            other_half = width - half_width
            right_half = image.crop((other_half, 0, width, height))
            left_half = right_half.transpose(PILImage.FLIP_LEFT_RIGHT)
            if other_half > 0:
                left_piece = left_half.crop((0, 0, other_half, height))
                if left_piece.mode in ("RGBA", "LA"):
                    result.paste(left_piece, (0, 0), left_piece)
                else:
                    result.paste(left_piece, (0, 0))
            if right_half.mode in ("RGBA", "LA"):
                result.paste(right_half, (other_half, 0), right_half)
            else:
                result.paste(right_half, (other_half, 0))
        elif mode == "top_to_bottom":
            half_height = (height + 1) // 2
            other_half = height - half_height
            top_half = image.crop((0, 0, width, half_height))
            bottom_half = top_half.transpose(PILImage.FLIP_TOP_BOTTOM)
            result.paste(top_half, (0, 0))
            if other_half > 0:
                bottom_piece = bottom_half.crop(
                    (0, bottom_half.height - other_half, width, bottom_half.height)
                )
                if bottom_piece.mode in ("RGBA", "LA"):
                    result.paste(bottom_piece, (0, half_height), bottom_piece)
                else:
                    result.paste(bottom_piece, (0, half_height))
        elif mode == "bottom_to_top":
            half_height = (height + 1) // 2
            other_half = height - half_height
            bottom_half = image.crop((0, other_half, width, height))
            top_half = bottom_half.transpose(PILImage.FLIP_TOP_BOTTOM)
            if other_half > 0:
                top_piece = top_half.crop((0, 0, width, other_half))
                if top_piece.mode in ("RGBA", "LA"):
                    result.paste(top_piece, (0, 0), top_piece)
                else:
                    result.paste(top_piece, (0, 0))
            if bottom_half.mode in ("RGBA", "LA"):
                result.paste(bottom_half, (0, other_half), bottom_half)
            else:
                result.paste(bottom_half, (0, other_half))
        else:
            return image.copy()
        return result

    def _rgba_to_gif_frame(
        self,
        image: PILImage.Image,
        palette_colors: int = 255,
        reserve_transparency: bool = True,
    ) -> PILImage.Image:
        if image.mode != "RGBA":
            image = image.convert("RGBA")
        alpha = image.getchannel("A")
        has_alpha = alpha.getextrema()[0] < 255
        colors = min(palette_colors, 255) if reserve_transparency else min(palette_colors, 256)
        p_frame = image.convert("RGB").quantize(colors=colors, method=1)
        if not reserve_transparency or not has_alpha:
            if reserve_transparency and not has_alpha:
                return p_frame
            return p_frame
        palette = p_frame.getpalette() or []
        if len(palette) < 768:
            palette.extend([0] * (768 - len(palette)))
        ti = self.GIF_TRANSPARENT_INDEX
        palette[ti * 3: ti * 3 + 3] = [0, 0, 0]
        p_frame.putpalette(palette)
        mask = alpha.point(lambda a: 255 if a < 128 else 0)
        p_frame.paste(ti, mask=mask)
        p_frame.info["transparency"] = ti
        return p_frame

    def process_mirror(self, img_data: bytes, mode: str):
        """镜像/反色：静态图输出 PNG；动图输出 GIF（保留时长与透明度）。"""
        mode_names = {
            "left_to_right": "左对称",
            "right_to_left": "右对称",
            "top_to_bottom": "上对称",
            "bottom_to_top": "下对称",
            "invert": "反色",
        }
        label = mode_names.get(mode, mode)
        try:
            img = self._open_checked(img_data)
            n_frames = int(getattr(img, "n_frames", 1) or 1)
            is_animated = bool(getattr(img, "is_animated", False)) or n_frames > 1

            if is_animated:
                frames = []
                durations = []
                loop = img.info.get("loop", 0)
                try:
                    loop = int(loop)
                except (TypeError, ValueError):
                    loop = 0
                max_frames = self.cfg.max_gif_frames
                for i, frame in enumerate(ImageSequence.Iterator(img)):
                    if i >= max_frames:
                        break
                    dur = frame.info.get("duration", 100)
                    try:
                        dur = int(dur)
                    except (TypeError, ValueError):
                        dur = 100
                    if dur <= 0:
                        dur = 100
                    durations.append(dur)
                    fr = self._compress_frame(frame.convert("RGBA"))
                    frames.append(self._apply_mirror(fr, mode))

                if not frames:
                    return f"❌ {label}失败：无帧", None, "gif"

                has_alpha = any(f.getchannel("A").getextrema()[0] < 255 for f in frames)
                gif_frames = [
                    self._rgba_to_gif_frame(f, reserve_transparency=has_alpha)
                    for f in frames
                ]
                output = io.BytesIO()
                save_kwargs = {
                    "format": "GIF",
                    "save_all": True,
                    "append_images": gif_frames[1:],
                    "duration": durations[: len(gif_frames)],
                    "loop": loop,
                    "disposal": 2,
                    "optimize": False,
                }
                if has_alpha:
                    save_kwargs["transparency"] = self.GIF_TRANSPARENT_INDEX
                gif_frames[0].save(output, **save_kwargs)
                output.seek(0)
                return f"✅ {label}完成（动图 {len(gif_frames)} 帧）", output, "gif"

            # 静态图
            if img.mode == "P":
                img = img.convert("RGBA")
            elif img.mode == "LA":
                img = img.convert("RGBA")
            elif img.mode not in ("RGB", "RGBA", "L"):
                img = img.convert("RGBA")
            img = self._compress_frame(img)
            result = self._apply_mirror(img, mode)
            out = io.BytesIO()
            if result.mode in ("RGBA", "LA") or (result.mode == "P" and "transparency" in result.info):
                if result.mode != "RGBA":
                    result = result.convert("RGBA")
                result.save(out, format="PNG")
                fmt = "png"
            else:
                if result.mode not in ("RGB", "L"):
                    result = result.convert("RGB")
                result.save(out, format="PNG")
                fmt = "png"
            out.seek(0)
            return f"✅ {label}完成", out, fmt
        except Exception as e:
            return f"❌ {label}失败: {e}", None, "png"

