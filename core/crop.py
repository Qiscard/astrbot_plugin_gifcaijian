# -*- coding: utf-8 -*-
"""Cropping helpers: auto border trim, equal grid, smart content-aware grid."""
from __future__ import annotations

import io
import re
from typing import Dict, List, Optional, Tuple

from PIL import Image as PILImage, ImageFilter
from astrbot.api import logger

from .config_helpers import Cfg
from .deps import np, has_numpy


class CropEngine:
    def __init__(self, cfg: Cfg):
        self.cfg = cfg


    def _auto_detect_crop(self, img: PILImage.Image, threshold: int = 240, mode: str = 'auto',
                          denoise: int = 3) -> tuple:
        """
        自动检测图片中的内容区域（去除白边/纯色背景），增强版：形态学降噪

        Args:
            img: PIL Image 对象
            threshold: 颜色阈值 (0-255)，小于此值视为背景
            mode: 检测模式 - 'auto'(自动), 'white'(白色背景), 'transparent'(透明背景)
            denoise: 形态学降噪核大小 (奇数, 1=关闭, 建议3-5)，用于过滤JPEG伪影和噪点

        Returns:
            (left, top, right, bottom) 裁剪边界
        """
        if np is None:
            return self._simple_auto_detect_crop(img, threshold)

        # --- Step 1: 构建内容掩码 ---
        if img.mode == 'RGBA':
            arr = np.array(img)
            if mode == 'transparent' or (mode == 'auto' and np.any(arr[:, :, 3] < 250)):
                # 透明背景检测：alpha > threshold 视为内容
                is_content = (arr[:, :, 3] > threshold)
            else:
                # 白色/纯色背景：任一通道显著低于阈值
                is_content = np.any(arr[:, :, :3] < threshold, axis=2)
        elif img.mode == 'P':
            arr = np.array(img.convert('RGB'))
            is_content = np.any(arr < threshold, axis=2)
        else:
            gray = img.convert('L')
            arr = np.array(gray)
            is_content = (arr < threshold)

        # --- Step 2: 形态学开运算去噪（腐蚀→膨胀） ---
        # 过滤 JPEG 压缩伪影、扫描噪点、水印等孤立噪点
        if denoise > 1:
            mask_img = PILImage.fromarray((is_content * 255).astype(np.uint8))
            # 腐蚀：消除孤立噪点（伪影区域通常不连续）
            eroded = mask_img.filter(ImageFilter.MinFilter(denoise))
            # 膨胀：恢复被腐蚀的内容边缘
            opened = eroded.filter(ImageFilter.MaxFilter(denoise))
            is_content = np.array(opened) > 128

        # --- Step 3: 找到内容边界 ---
        rows = np.any(is_content, axis=1)
        cols = np.any(is_content, axis=0)

        if not np.any(rows) or not np.any(cols):
            return (0, 0, img.width, img.height)

        top = np.argmax(rows)
        bottom = len(rows) - np.argmax(rows[::-1])
        left = np.argmax(cols)
        right = len(cols) - np.argmax(cols[::-1])

        # 安全边距（防止裁到照片边缘）
        safe_margin = 2
        left = max(0, left - safe_margin)
        top = max(0, top - safe_margin)
        right = min(img.width, right + safe_margin)
        bottom = min(img.height, bottom + safe_margin)

        return (left, top, right, bottom)
    

    def _simple_auto_detect_crop(self, img: PILImage.Image, threshold: int = 240) -> tuple:
        """简单的白边检测（不使用numpy）"""
        # 转换为RGB模式
        if img.mode != 'RGB':
            img = img.convert('RGB')
        
        w, h = img.size
        left, top, right, bottom = w, h, 0, 0
        
        # 扫描所有像素（效率较低，仅作备选）
        for y in range(h):
            for x in range(w):
                r, g, b = img.getpixel((x, y))
                # 如果不是白色/接近白色
                if r < threshold or g < threshold or b < threshold:
                    left = min(left, x)
                    top = min(top, y)
                    right = max(right, x)
                    bottom = max(bottom, y)
        
        if left > right or top > bottom:
            return (0, 0, w, h)
        
        return (left, top, right + 1, bottom + 1)
    

    def _worker_auto_crop(self, img_data: bytes, threshold: int, mode: str = 'auto',
                          denoise: int = 3, min_crop_ratio: float = 0.90) -> tuple:
        """自动裁切工作线程"""
        try:
            img = PILImage.open(io.BytesIO(img_data))
            original_w, original_h = img.size

            # 执行自动检测（带降噪）
            left, top, right, bottom = self._auto_detect_crop(img, threshold, mode, denoise)

            # 计算裁剪后的尺寸
            new_w = right - left
            new_h = bottom - top

            # 如果裁剪后尺寸变化很小，说明几乎没有白边
            if new_w >= original_w * min_crop_ratio and new_h >= original_h * min_crop_ratio:
                info = f"⚠️ 未检测到明显白边\n"
                info += f"原图: {original_w}x{original_h}\n"
                info += f"提示：可尝试降低阈值（当前{threshold}）或使用 \"自动裁切 {threshold-20}\""
                return img_data, info
            
            # 执行裁剪
            cropped = img.crop((left, top, right, bottom))
            
            # 判断是否为排版图（多张照片）
            aspect_ratio = new_w / new_h
            
            output = io.BytesIO()
            # 根据输出格式保存
            output_fmt = self.cfg.get('crop_output_format', 'PNG').upper()
            if output_fmt == 'JPEG' or output_fmt == 'JPG':
                cropped = cropped.convert('RGB')
                cropped.save(output, format='JPEG', quality=90)
            else:
                cropped.save(output, format='PNG')
            
            # 生成详细信息
            info = f"✅ 自动裁切完成\n"
            info += f"原图: {original_w}x{original_h}\n"
            info += f"裁后: {new_w}x{new_h}\n"
            info += f"去除白边: 上{top}px 下{original_h-bottom}px 左{left}px 右{original_w-right}px\n"
            info += f"阈值: {threshold} | 模式: {mode}"
            
            # 提示是否为排版图
            if 0.3 < aspect_ratio < 0.8 and new_h > new_w:
                info += f"\n💡 检测到竖版图片，可能是单人照"
            elif 0.8 < aspect_ratio < 1.5 and min(new_w, new_h) > 500:
                info += f"\n💡 可能是排版图！如需分割，请继续使用 \"裁剪 行x列\" 命令"
            
            return output.getvalue(), info
        except Exception as e:
            import traceback
            return None, f"❌ 裁切失败: {repr(e)}\n{traceback.format_exc()}"


    def _parse_margins(self, text: str):
        margins = {'top': 0, 'bottom': 0, 'left': 0, 'right': 0}
        pattern = r'边距\s*([上下左右])?边?\s*(\d+)'
        matches = re.findall(pattern, text)
        for direction, amount_str in matches:
            try:
                amount = int(amount_str)
                if not direction:
                    for k in margins: 
                        margins[k] += amount
                elif direction == '上':
                    margins['top'] += amount
                elif direction == '下':
                    margins['bottom'] += amount
                elif direction == '左':
                    margins['left'] += amount
                elif direction == '右':
                    margins['right'] += amount
            except ValueError:
                pass
        clean_text = re.sub(pattern, " ", text)
        return clean_text, margins


    def _crop_image_data(self, img_data: bytes, margins: dict) -> tuple[bytes, str]:
        if all(v == 0 for v in margins.values()): 
            return img_data, ""
        try:
            img = PILImage.open(io.BytesIO(img_data)).convert("RGBA")
            w, h = img.size
            l, u, r, d = margins['left'], margins['top'], w - margins['right'], h - margins['bottom']
            if l >= r or u >= d: 
                return img_data, f"\n⚠️ 边距无效: {w}x{h} -> {l},{u},{r},{d}"
            output = io.BytesIO()
            img.crop((l, u, r, d)).save(output, format='PNG')
            return output.getvalue(), f"\n✂️ 已裁边距: 上{margins['top']} 下{margins['bottom']} 左{margins['left']} 右{margins['right']}"
        except Exception as e:
            return img_data, f"\n⚠️ 边距裁剪出错: {e}"

    # --- 网格裁剪功能 ---

    def _worker_crop_grid(self, img_data: bytes, margins: dict, rows: int, cols: int):
        img_data, crop_msg = self._crop_image_data(img_data, margins)
        try:
            img = PILImage.open(io.BytesIO(img_data)).convert("RGBA")
            w, h = img.size
            cw, ch = w // cols, h // rows
            if cw < 1 or ch < 1: 
                return f"❌ 图片太小 {crop_msg}", None
            res_list = []
            for r in range(rows):
                for c in range(cols):
                    out = io.BytesIO()
                    img.crop((c * cw, r * ch, (c + 1) * cw, (r + 1) * ch)).save(out, format='PNG')
                    res_list.append(out.getvalue())
            return crop_msg, res_list
        except Exception as e:
            return f"❌ 出错: {e}", None


    def _build_content_mask(self, img: PILImage.Image, threshold: int = 248):
        """True = content. Keep light FX; only pure near-white/transparent is bg."""
        if not has_numpy():
            return None
        arr = np.array(img.convert("RGBA"))
        rgb = arr[:, :, :3].astype(np.int16)
        alpha = arr[:, :, 3]
        chroma = rgb.max(axis=2) - rgb.min(axis=2)
        near_white = (rgb.min(axis=2) >= threshold) & (chroma <= 12)
        bg = near_white | (alpha < 10)
        content = ~bg
        # light open to drop JPEG speckles without erasing thin lines
        if content.any():
            content = self._morph_open(content, k=3)
        return content


    def _morph_open(self, mask, k: int = 3):
        if k < 3:
            return mask
        m = mask.astype(np.uint8)
        h, w = m.shape
        pad = k // 2
        p = np.pad(m, pad, mode="constant", constant_values=0)
        # erode
        e = np.ones_like(m)
        for dy in range(k):
            for dx in range(k):
                e = np.minimum(e, p[dy:dy + h, dx:dx + w])
        p2 = np.pad(e, pad, mode="constant", constant_values=0)
        d = np.zeros_like(m)
        for dy in range(k):
            for dx in range(k):
                d = np.maximum(d, p2[dy:dy + h, dx:dx + w])
        return d.astype(bool)


    def _smooth_1d(self, x, win: int = 11):
        win = max(3, int(win) | 1)
        kernel = np.ones(win, dtype=float) / win
        return np.convolve(x.astype(float), kernel, mode="same")


    def _equal_bounds(self, length: int, n: int):
        step = length / n
        return [(int(round(i * step)), int(round((i + 1) * step))) for i in range(n)]


    def _find_runs(self, active):
        """active: 1d bool. return list of (start, end exclusive)."""
        runs = []
        i = 0
        n = len(active)
        while i < n:
            if not active[i]:
                i += 1
                continue
            j = i + 1
            while j < n and active[j]:
                j += 1
            runs.append((i, j))
            i = j
        return runs


    def _split_run_by_valleys(self, smooth, start: int, end: int, n_parts: int):
        """Split one merged band into n_parts using weakest internal valleys."""
        if n_parts <= 1 or end - start < n_parts * 6:
            step = max(1, (end - start) // max(1, n_parts))
            cuts = [start + i * step for i in range(n_parts)] + [end]
            return [(cuts[i], cuts[i + 1]) for i in range(n_parts)]

        seg = smooth[start:end]
        # local valleys inside segment (not edges)
        margin = max(2, (end - start) // (n_parts * 6))
        candidates = []
        for i in range(margin, len(seg) - margin):
            v = float(seg[i])
            if v <= float(seg[i - 1]) and v <= float(seg[i + 1]):
                left_peak = float(seg[max(0, i - margin * 3):i].max())
                right_peak = float(seg[i + 1:min(len(seg), i + margin * 3 + 1)].max())
                depth = min(left_peak, right_peak) - v
                # prefer deeper valleys; slight preference for more centered cuts among n_parts
                candidates.append((depth, i + start, v))
        candidates.sort(key=lambda t: t[0], reverse=True)

        min_gap = max(4, (end - start) // (n_parts * 4))
        chosen = []
        for depth, pos, _v in candidates:
            if all(abs(pos - c) >= min_gap for c in chosen):
                chosen.append(pos)
            if len(chosen) >= n_parts - 1:
                break

        # fill missing with equal-ish positions in segment
        if len(chosen) < n_parts - 1:
            for k in range(1, n_parts):
                exp = start + int(round(k * (end - start) / n_parts))
                if all(abs(exp - c) >= max(2, min_gap // 2) for c in chosen):
                    # snap to local min near expected
                    lo = max(start + 1, exp - min_gap)
                    hi = min(end - 1, exp + min_gap)
                    if lo < hi:
                        local = smooth[lo:hi]
                        exp = lo + int(np.argmin(local))
                    chosen.append(exp)
                if len(chosen) >= n_parts - 1:
                    break

        chosen = sorted(chosen)[: n_parts - 1]
        cuts = [start] + chosen + [end]
        return [(cuts[i], cuts[i + 1]) for i in range(len(cuts) - 1)]




    def _detect_1d_parts(self, proj, n_parts: int, axis_name: str = "axis"):
        """
        Detect n content bands on a 1D projection.

        AI sticker sheets often have:
        - uneven spacing
        - dirty gutters (hair/FX leak into white gaps)
        so pure "active > thr" runs merge, and midpoint cuts drift into the next row.

        Strategy order:
        1) Cluster high-content coordinates (1D k-means) -> robust band ranges
        2) Cut at gap midpoints between clusters
        3) Fallback: band runs / valleys / equal
        """
        length = len(proj)
        if n_parts <= 1:
            return [(0, length)], "single"

        raw = proj.astype(float)
        peak = float(raw.max()) if length else 0.0
        if peak <= 1e-6:
            return self._equal_bounds(length, n_parts), "equal-empty"

        # --- 1) cluster high-content positions ---
        cluster_bounds = self._cluster_bounds(raw, n_parts)
        if cluster_bounds is not None:
            return cluster_bounds, "clusters"

        # --- 2) classic runs (clean pure-white gutters) ---
        win = max(3, min(9, length // 150) | 1)
        smooth = self._smooth_1d(raw, win=win)
        thr = max(peak * 0.12, 1.0)
        thr = min(thr, peak * 0.55)
        active = raw > thr
        active = self._fill_small_holes_1d(active, max_hole=2)
        runs = [(a, b) for a, b in self._find_runs(active) if (b - a) >= max(2, length // (n_parts * 40))]

        if len(runs) == n_parts:
            return self._bands_to_bounds(runs, length), "bands-exact"

        if len(runs) > n_parts:
            items = [list(r) for r in runs]
            while len(items) > n_parts:
                best_i, best_gap = 0, 10**9
                for i in range(len(items) - 1):
                    gap = items[i + 1][0] - items[i][1]
                    if gap < best_gap:
                        best_gap = gap
                        best_i = i
                items[best_i][1] = items[best_i + 1][1]
                items.pop(best_i + 1)
            return self._bands_to_bounds([(a, b) for a, b in items], length), "bands-merged"

        # --- 3) valleys on residual-dirty gaps ---
        valley_bounds = self._valley_bounds(raw, smooth, n_parts)
        # Prefer valleys that land in low-density zones
        if self._bounds_look_ok(valley_bounds, raw, n_parts):
            return valley_bounds, "valleys"

        if len(runs) >= 1:
            deficits = n_parts - len(runs)
            widths = [b - a for a, b in runs]
            split_counts = [1] * len(runs)
            for _ in range(max(0, deficits)):
                scores = [widths[i] / split_counts[i] for i in range(len(runs))]
                split_counts[int(np.argmax(scores))] += 1
            bounds = []
            for (a, b), sc in zip(runs, split_counts):
                bounds.extend(self._split_run_by_valleys(smooth, a, b, sc))
            bounds = self._normalize_bounds(bounds, length, n_parts)
            if self._bounds_look_ok(bounds, raw, n_parts):
                return bounds, "bands-split"

        return valley_bounds, "valleys-fallback"


    def _cluster_bounds(self, raw, n_parts: int):
        """1D k-means on high-content indices; cuts at mid-gaps between clusters."""
        length = len(raw)
        peak = float(raw.max())
        if peak <= 0:
            return None

        # Use a moderately high threshold so only real character bands join clusters.
        # Dirty gutter residual stays below this.
        thr = max(peak * 0.30, float(np.percentile(raw, 55)) * 0.9)
        thr = min(thr, peak * 0.55)
        ys = np.where(raw >= thr)[0]
        if len(ys) < n_parts * 8:
            # lower thr once
            thr = max(peak * 0.18, 1.0)
            ys = np.where(raw >= thr)[0]
        if len(ys) < n_parts * 5:
            return None

        vals = ys.astype(float)
        # init centers by quantiles
        cents = np.array(
            [np.quantile(vals, (i + 0.5) / n_parts) for i in range(n_parts)],
            dtype=float,
        )
        for _ in range(30):
            dist = np.abs(vals[:, None] - cents[None, :])
            lab = dist.argmin(axis=1)
            new_cents = cents.copy()
            for i in range(n_parts):
                if (lab == i).any():
                    new_cents[i] = vals[lab == i].mean()
            if np.allclose(new_cents, cents, atol=0.5):
                cents = new_cents
                break
            cents = new_cents

        order = np.argsort(cents)
        cents = cents[order]
        # remap labels to sorted centers
        dist = np.abs(vals[:, None] - cents[None, :])
        lab = dist.argmin(axis=1)

        ranges = []
        for i in range(n_parts):
            pts = ys[lab == i]
            if len(pts) == 0:
                return None
            ranges.append((int(pts.min()), int(pts.max()) + 1))

        # clusters must be ordered and mostly non-overlapping
        for i in range(n_parts - 1):
            if ranges[i][0] >= ranges[i + 1][0]:
                return None
            # allow slight overlap but not heavy
            if ranges[i][1] - ranges[i + 1][0] > max(8, length // 80):
                # too overlapped
                return None

        # gap midpoints; if touching/overlapping, cut at contact with bias BEFORE next band start
        cuts = [0]
        for i in range(n_parts - 1):
            end_i = ranges[i][1]
            start_j = ranges[i + 1][0]
            if start_j > end_i:
                # clean or dirty gap: snap cut to lowest-density position inside gap
                lo, hi = end_i, start_j
                if hi - lo >= 3:
                    window = raw[lo:hi]
                    # prefer deepest valley; if flat, midpoint
                    pos = lo + int(np.argmin(window))
                    # if multiple near-min, take middle of low band
                    min_v = float(window.min())
                    thr_v = min_v + max(1.0, 0.08 * (float(window.max()) - min_v + 1.0))
                    band = np.where(window <= thr_v)[0]
                    if len(band):
                        pos = lo + int((band[0] + band[-1]) / 2)
                else:
                    pos = (end_i + start_j) // 2
            else:
                # overlap/touch: cut just before next cluster content peak
                # search local min near contact
                mid = (end_i + start_j) // 2
                lo = max(1, mid - max(6, length // 100))
                hi = min(length - 1, mid + max(6, length // 100))
                pos = lo + int(np.argmin(raw[lo:hi]))
            cuts.append(int(pos))
        cuts.append(length)

        # ensure strictly increasing
        for i in range(1, len(cuts)):
            if cuts[i] <= cuts[i - 1]:
                cuts[i] = min(length, cuts[i - 1] + 1)
        cuts[-1] = length
        bounds = [(cuts[i], cuts[i + 1]) for i in range(n_parts)]

        if not self._bounds_look_ok(bounds, raw, n_parts):
            return None
        return bounds


    def _bounds_look_ok(self, bounds, raw, n_parts: int) -> bool:
        if not bounds or len(bounds) != n_parts:
            return False
        length = len(raw)
        sizes = [b - a for a, b in bounds]
        if min(sizes) < max(4, length // (n_parts * 8)):
            return False
        # each band should contain a decent share of total content
        total = float(raw.sum()) + 1e-6
        shares = [float(raw[a:b].sum()) / total for a, b in bounds]
        if min(shares) < 0.10 / max(1, n_parts - 1) and n_parts <= 4:
            # allow a bit uneven but not empty-ish
            if min(shares) < 0.08:
                return False
        # cuts should not sit on peak content (bleed indicator)
        peak = float(raw.max()) + 1e-6
        for i in range(1, n_parts):
            cut = bounds[i][0]
            # sample neighborhood
            lo = max(0, cut - 2)
            hi = min(length, cut + 3)
            if float(raw[lo:hi].mean()) > peak * 0.70:
                return False
        return True


    def _fill_small_holes_1d(self, active, max_hole: int = 2):
        a = active.copy()
        n = len(a)
        i = 0
        while i < n:
            if a[i]:
                i += 1
                continue
            j = i
            while j < n and not a[j]:
                j += 1
            left_on = i > 0 and a[i - 1]
            if left_on and j < n and (j - i) <= max_hole:
                a[i:j] = True
            i = j if j > i else i + 1
        return a


    def _valley_bounds(self, raw, smooth, n_parts: int):
        length = len(raw)
        if n_parts <= 1:
            return [(0, length)]

        # Combine raw + smooth; tiny residual gutters still have lower raw.
        score = 0.65 * raw + 0.35 * smooth
        # Also use "drop from local max" signal
        min_gap = max(6, length // (n_parts * 5))
        candidates = []
        for i in range(min_gap, length - min_gap):
            v = float(score[i])
            if v <= float(score[i - 1]) and v <= float(score[i + 1]):
                left_peak = float(score[max(0, i - min_gap): i].max())
                right_peak = float(score[i + 1: min(length, i + min_gap + 1)].max())
                depth = min(left_peak, right_peak) - v
                # strong preference for absolute low density
                abs_pen = v / (float(score.max()) + 1e-6)
                sc = depth - 0.35 * abs_pen * (float(score.max()) + 1)
                candidates.append((sc, i, v, depth))

        candidates.sort(key=lambda t: t[0], reverse=True)
        chosen = []
        min_depth = max(1.0, float(score.max()) * 0.03)
        for sc, i, v, depth in candidates:
            if depth < min_depth:
                continue
            # reject cuts that are still high density (inside character)
            if v > float(score.max()) * 0.55:
                continue
            if all(abs(i - c) >= min_gap for c in chosen):
                chosen.append(i)
            if len(chosen) >= n_parts - 1:
                break

        if len(chosen) < n_parts - 1:
            # fill with equal snapped to lowest density
            for k in range(1, n_parts):
                exp = int(round(k * length / n_parts))
                if any(abs(exp - c) < min_gap for c in chosen):
                    continue
                lo = max(1, exp - min_gap)
                hi = min(length - 1, exp + min_gap)
                pos = lo + int(np.argmin(score[lo:hi]))
                if all(abs(pos - c) >= max(3, min_gap // 2) for c in chosen):
                    chosen.append(pos)
                if len(chosen) >= n_parts - 1:
                    break

        if len(chosen) < n_parts - 1:
            return self._equal_bounds(length, n_parts)

        chosen = sorted(chosen)[: n_parts - 1]
        cuts = [0] + chosen + [length]
        return [(cuts[i], cuts[i + 1]) for i in range(n_parts)]


    def _bands_to_bounds(self, runs, length: int):
        """Convert content runs to exclusive cell bounds using midpoints of gutters."""
        n = len(runs)
        if n == 0:
            return [(0, length)]
        cuts = [0]
        for i in range(n - 1):
            gap_start = runs[i][1]
            gap_end = runs[i + 1][0]
            if gap_end <= gap_start:
                # overlapping / touching: cut at contact, bias to leave previous content intact
                mid = gap_start
            else:
                mid = (gap_start + gap_end) // 2
            cuts.append(int(mid))
        cuts.append(length)
        # ensure monotonic increasing exclusive ranges
        bounds = []
        for i in range(n):
            s = cuts[i]
            e = cuts[i + 1]
            if e <= s:
                e = min(length, s + 1)
            bounds.append((s, e))
        # stretch first/last to edges
        bounds[0] = (0, bounds[0][1])
        bounds[-1] = (bounds[-1][0], length)
        return bounds


    def _normalize_bounds(self, bounds, length: int, n_parts: int):
        if not bounds:
            return self._equal_bounds(length, n_parts)
        # if wrong count, fall back
        if len(bounds) != n_parts:
            # crop or pad
            if len(bounds) > n_parts:
                bounds = bounds[:n_parts]
                bounds[-1] = (bounds[-1][0], length)
            else:
                while len(bounds) < n_parts:
                    # split last
                    a, b = bounds[-1]
                    mid = (a + b) // 2
                    bounds[-1] = (a, mid)
                    bounds.append((mid, b))
        bounds[0] = (0, bounds[0][1])
        bounds[-1] = (bounds[-1][0], length)
        # fix overlaps / gaps
        fixed = []
        for i, (s, e) in enumerate(bounds):
            if i == 0:
                s = 0
            else:
                s = fixed[-1][1]
            if i == len(bounds) - 1:
                e = length
            e = max(s + 1, e)
            fixed.append((s, e))
        fixed[-1] = (fixed[-1][0], length)
        return fixed


    def _detect_grid_bounds(self, img: PILImage.Image, rows: int, cols: int, threshold: int = 248):
        w, h = img.size
        mask = self._build_content_mask(img, threshold=threshold)
        if mask is None or not mask.any():
            return (
                self._equal_bounds(h, rows),
                self._equal_bounds(w, cols),
                mask,
                "equal-fallback",
            )

        row_proj = mask.sum(axis=1).astype(float)
        col_proj = mask.sum(axis=0).astype(float)

        row_bounds, row_mode = self._detect_1d_parts(row_proj, rows, "row")
        col_bounds, col_mode = self._detect_1d_parts(col_proj, cols, "col")

        # Safety: if any band is absurdly small, re-equal that axis
        min_row = max(4, h // (rows * 8))
        min_col = max(4, w // (cols * 8))
        if any((b - a) < min_row for a, b in row_bounds):
            row_bounds = self._equal_bounds(h, rows)
            row_mode = "equal-safe-row"
        if any((b - a) < min_col for a, b in col_bounds):
            col_bounds = self._equal_bounds(w, cols)
            col_mode = "equal-safe-col"

        mode = f"row:{row_mode}|col:{col_mode}"
        return row_bounds, col_bounds, mask, mode


    def _trim_cell_inplace(self, cell: PILImage.Image, mask_cell, pad: int = 2, max_trim_ratio: float = 0.18):
        """Trim white inside cell only; never expand; cap max trim so we don't over-crop."""
        if mask_cell is None or not has_numpy() or not mask_cell.any():
            return cell
        rows_on = np.any(mask_cell, axis=1)
        cols_on = np.any(mask_cell, axis=0)
        if not rows_on.any() or not cols_on.any():
            return cell
        y0, y1 = np.where(rows_on)[0][[0, -1]]
        x0, x1 = np.where(cols_on)[0][[0, -1]]
        h, w = mask_cell.shape
        x0 = max(0, int(x0) - pad)
        y0 = max(0, int(y0) - pad)
        x1 = min(w, int(x1) + 1 + pad)
        y1 = min(h, int(y1) + 1 + pad)

        # cap how much we trim from each side so partial-bleed remnants aren't chased away wrongly
        max_dx = int(w * max_trim_ratio)
        max_dy = int(h * max_trim_ratio)
        if x0 > max_dx:
            x0 = max_dx
        if (w - x1) > max_dx:
            x1 = w - max_dx
        if y0 > max_dy:
            y0 = max_dy
        if (h - y1) > max_dy:
            y1 = h - max_dy

        if (x1 - x0) < w * 0.45 or (y1 - y0) < h * 0.45:
            return cell
        if x0 <= 1 and y0 <= 1 and x1 >= w - 1 and y1 >= h - 1:
            return cell
        return cell.crop((x0, y0, x1, y1))


    def smart_grid_split(
        self,
        img_data: bytes,
        rows: int,
        cols: int,
        margins=None,
        threshold: int = 248,
        auto_clean: bool = True,
        clean_threshold: int = 240,
        denoise: int = 3,
    ):
        """智能网格裁剪：内容条带聚类找缝 + 格内去白边。

适合 AI 九宫格（行距不均、缝脏/极窄）。失败时内部回退均分。"""
        try:
            margins = margins or {"top": 0, "bottom": 0, "left": 0, "right": 0}
            processed, crop_msg = self._crop_image_data(img_data, margins)
            img = PILImage.open(io.BytesIO(processed)).convert("RGBA")
            w, h = img.size
            if rows < 1 or cols < 1 or rows * cols > 100:
                return "❌ 行列不合法", None, ""

            if not has_numpy():
                msg, blist = self._worker_crop_grid(
                    processed, {"top": 0, "bottom": 0, "left": 0, "right": 0}, rows, cols
                )
                return f"{msg}\n⚠️ 无 numpy，已回退均分网格", blist, crop_msg

            row_bounds, col_bounds, mask, mode = self._detect_grid_bounds(
                img, rows, cols, threshold=threshold
            )

            result_list = []
            row_heights = []
            col_widths = []
            for r in range(rows):
                y0, y1 = int(row_bounds[r][0]), int(row_bounds[r][1])
                y1 = max(y0 + 1, min(h, y1))
                row_heights.append(y1 - y0)
                for c in range(cols):
                    x0, x1 = int(col_bounds[c][0]), int(col_bounds[c][1])
                    x1 = max(x0 + 1, min(w, x1))
                    if r == 0:
                        col_widths.append(x1 - x0)

                    # exclusive crop, no neighbor padding
                    cell = img.crop((x0, y0, x1, y1))
                    if auto_clean and mask is not None:
                        cell = self._trim_cell_inplace(cell, mask[y0:y1, x0:x1])

                    out = io.BytesIO()
                    cell.save(out, format="PNG")
                    result_list.append(out.getvalue())

            report = (
                f"📊 智能裁剪报告\n"
                f"原图: {w}x{h} → {rows}行 x {cols}列\n"
                f"模式: {mode}\n"
                f"行高: {row_heights}\n"
                f"列宽: {col_widths}\n"
                f"{crop_msg}\n"
                f"✅ 成功提取 {len(result_list)} 张（内容聚类缝检测，抗AI行距不均/防串行）"
            )
            return report, result_list, crop_msg
        except Exception as e:
            import traceback
            return f"❌ 智能裁剪失败: {repr(e)}\n{traceback.format_exc()}", None, ""

