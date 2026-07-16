# -*- coding: utf-8 -*-
"""Media URL extraction, download and video source resolution."""
from __future__ import annotations

import asyncio
import io
import os
from typing import Optional, List, Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.utils.io import download_image_by_url, download_file as astrbot_download_file

from .config_helpers import Cfg


class MediaHelper:
    def __init__(self, cfg: Cfg):
        self.cfg = cfg

    def _get_image_url(self, event: AstrMessageEvent) -> str:
        """获取目标图片URL：优先回复的图片 -> 当前消息的图片 -> At对象的头像"""

        # 1. 检查回复链中的图片（优先）
        if hasattr(event.message_obj, "message"):
            for seg in event.message_obj.message:
                if isinstance(seg, Comp.Reply) and seg.chain and isinstance(seg.chain, list):
                    reply_urls = self._extract_images_from_chain(seg.chain)
                    if reply_urls:
                        return reply_urls[0]

        # 2. 检查当前消息中的图片
        if hasattr(event.message_obj, "message") and isinstance(event.message_obj.message, list):
            urls = self._extract_images_from_chain(event.message_obj.message)
            if urls:
                return urls[0]

        # 3. 补充 get_images
        if hasattr(event, "get_images"):
            images = event.get_images()
            if images and len(images) > 0 and hasattr(images[0], 'url') and images[0].url:
                return images[0].url

        # 4. 检查 At (获取头像)
        if hasattr(event.message_obj, "message") and isinstance(event.message_obj.message, list):
            for seg in event.message_obj.message:
                if isinstance(seg, Comp.At):
                    user_id = str(seg.qq)
                    return f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"

        return None

    # --- 递归提取所有图片 (支持合并转发、回复等) ---
    def _extract_images_from_chain(self, chain: list) -> list[str]:
        """递归提取消息链中的所有图片URL"""
        urls = []
        if not isinstance(chain, list):
            return urls
            
        for item in chain:
            # 1. 直接是 Image 组件
            if isinstance(item, Comp.Image) and item.url:
                urls.append(item.url)
            # 2. 字典格式
            elif isinstance(item, dict):
                if item.get('type') == 'image':
                    url = item.get('data', {}).get('url') or item.get('url') or item.get('file')
                    if url and isinstance(url, str) and url.startswith('http'):
                        urls.append(url)
                # 3. 嵌套节点 (Forward Node)
                elif item.get('type') == 'node':
                    content = item.get('data', {}).get('content') or item.get('content')
                    if isinstance(content, list):
                        urls.extend(self._extract_images_from_chain(content))
            # 4. Reply 组件
            elif isinstance(item, Comp.Reply) and item.chain and isinstance(item.chain, list):
                urls.extend(self._extract_images_from_chain(item.chain))
            # 5. Nodes 组件
            elif isinstance(item, Comp.Nodes) and item.nodes:
                for node in item.nodes:
                    if hasattr(node, 'content') and isinstance(node.content, list):
                        urls.extend(self._extract_images_from_chain(node.content))
        return urls

    async def _get_all_image_urls(self, event: AstrMessageEvent) -> list[str]:
        """获取上下文中所有的图片链接（包括当前消息、回复的消息、转发消息、At头像）"""
        urls = []

        # 1. 检查 event.message_obj.message
        if hasattr(event.message_obj, "message") and isinstance(event.message_obj.message, list):
            urls.extend(self._extract_images_from_chain(event.message_obj.message))

        # 2. 补充 get_images
        if hasattr(event, "get_images"):
            imgs = event.get_images()
            for img in imgs:
                if hasattr(img, 'url') and img.url and img.url not in urls:
                    urls.append(img.url)
        
        # 3. 补充 At 头像
        if hasattr(event.message_obj, "message") and isinstance(event.message_obj.message, list):
            for seg in event.message_obj.message:
                if isinstance(seg, Comp.At):
                    uid = str(seg.qq)
                    url = f"https://q1.qlogo.cn/g?b=qq&nk={uid}&s=640"
                    if url not in urls:
                        urls.append(url)

        # 去重但保持顺序
        seen = set()
        unique_urls = []
        for u in urls:
            if u not in seen:
                unique_urls.append(u)
                seen.add(u)
        return unique_urls

    # --- 辅助方法: 智能获取视频源 ---
    def _get_video_source(self, event: AstrMessageEvent) -> str:
        candidates = []

        def extract_from_item(item):
            url = getattr(item, 'url', None)
            if not url and isinstance(item, dict):
                url = item.get('data', {}).get('url') or item.get('url')
            if url and isinstance(url, str) and url.startswith('http'):
                return 100, url
            path = getattr(item, 'path', None)
            if not path and isinstance(item, dict):
                path = item.get('data', {}).get('path') or item.get('path')
            if path and isinstance(path, str) and os.path.isabs(path) and os.path.exists(path):
                return 90, path
            file_info = getattr(item, 'file', None)
            if not file_info and isinstance(item, dict):
                file_info = item.get('data', {}).get('file') or item.get('file')
            if file_info and isinstance(file_info, str):
                return 50, file_info
            return 0, None

        items_to_check = []
        if hasattr(event, "get_videos"):
            videos = event.get_videos()
            if videos: 
                items_to_check.extend(videos)

        if hasattr(event.message_obj, "message") and isinstance(event.message_obj.message, list):
            for seg in event.message_obj.message:
                if isinstance(seg, Comp.Reply) and seg.chain and isinstance(seg.chain, list):
                    items_to_check.extend(seg.chain)
                elif isinstance(seg, (Comp.Video, dict)):
                    items_to_check.append(seg)
                elif isinstance(seg, dict) and seg.get('type') == 'video':
                    items_to_check.append(seg)

        for item in items_to_check:
            score, val = extract_from_item(item)
            if val: 
                candidates.append((score, val))

        if not candidates: 
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    # --- 通过API解析文件ID ---
    async def _resolve_file_via_api(self, event: AstrMessageEvent, file_id: str) -> str:
        try:
            logger.info(f"尝试通过API解析文件ID: {file_id}")
            res = await event.bot.api.call_action("get_file", file_id=file_id)
            if not res or not isinstance(res, dict): 
                return None
            url = res.get('url')
            if url and url.startswith('http'): 
                return url
            path = res.get('file')
            if path and os.path.exists(path): 
                return path
            return url or path
        except Exception as e:
            logger.warning(f"API解析文件失败: {e}")
            return None

    async def _download_content(self, url: str) -> Optional[bytes]:
        """下载图片/视频为 bytes。

        优先复用 AstrBot 内置的 download_image_by_url，它处理了 certifi CA、
        trust_env(系统代理)、SSL 失败降级等场景，比裸 aiohttp 更可靠
        （QQ 图床 CDN 在 Windows 下常因证书链/代理导致裸 aiohttp 静默失败）。
        """
        if not url or not isinstance(url, str):
            return None
        if not url.startswith(('http://', 'https://')):
            return None

        max_size = int(self.cfg.get('max_download_size_mb', 50) * 1024 * 1024)
        try:
            # download_image_by_url 返回本地临时文件路径
            local_path = await asyncio.wait_for(
                download_image_by_url(url),
                timeout=180,
            )
        except Exception as e:
            logger.warning(f"图片下载失败({url[:80]}): {e}")
            return None

        try:
            if not local_path or not os.path.exists(local_path):
                return None
            size = os.path.getsize(local_path)
            if size > max_size:
                logger.warning(f"下载文件过大: {size} > {max_size} - {url[:80]}")
                return None
            with open(local_path, 'rb') as f:
                return f.read()
        except Exception as e:
            logger.warning(f"读取下载文件失败: {e}")
            return None
        finally:
            # 清理临时文件
            try:
                if local_path and os.path.exists(local_path) and 'temp' in local_path.lower():
                    os.remove(local_path)
            except Exception:
                pass

    def _get_first_image_component(self, event: AstrMessageEvent):
        """获取首个 Image 组件对象（用于 URL 失效时走 convert_to_file_path 回退）。"""
        def find_in_chain(chain):
            if not isinstance(chain, list):
                return None
            for item in chain:
                if isinstance(item, Comp.Image) and (item.url or item.file):
                    return item
                if isinstance(item, Comp.Reply) and item.chain:
                    found = find_in_chain(item.chain)
                    if found:
                        return found
                if isinstance(item, Comp.Nodes) and item.nodes:
                    for node in item.nodes:
                        if hasattr(node, 'content') and isinstance(node.content, list):
                            found = find_in_chain(node.content)
                            if found:
                                return found
            return None

        if hasattr(event.message_obj, "message") and isinstance(event.message_obj.message, list):
            comp = find_in_chain(event.message_obj.message)
            if comp:
                return comp
        if hasattr(event, "get_images"):
            imgs = event.get_images()
            if imgs:
                return imgs[0]
        return None

    async def _resolve_image_bytes(self, event: AstrMessageEvent) -> Optional[bytes]:
        """可靠的图片字节获取：先 HTTP 下载 URL；失败则用 Image 组件的
        convert_to_file_path() 走 MediaResolver 回退（可处理 base64/file_id/本地路径，
        且当回复的是旧消息、QQ CDN URL 已失效时仍能通过协议端重新拉取）。"""
        # 1. 先尝试 HTTP 直链
        url = self._get_image_url(event)
        if url:
            data = await self._download_content(url)
            if data:
                return data
            logger.warning(f"URL 下载失败，尝试通过协议端重新解析图片: {url[:80]}")

        # 2. 回退：用 Image 组件走 MediaResolver（依赖协议端能力）
        comp = self._get_first_image_component(event)
        if comp is None:
            return None
        try:
            path = await asyncio.wait_for(comp.convert_to_file_path(), timeout=180)
            max_size = int(self.cfg.get('max_download_size_mb', 50) * 1024 * 1024)
            if path and os.path.exists(path):
                if os.path.getsize(path) > max_size:
                    logger.warning(f"图片过大: {os.path.getsize(path)} > {max_size}")
                    return None
                with open(path, 'rb') as f:
                    return f.read()
        except Exception as e:
            logger.warning(f"协议端解析图片失败: {e}")
        return None

