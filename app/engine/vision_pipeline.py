"""
DeepSeek 识图与多模态管道 (Vision Pipeline)
负责 Base64 / URL 图片在内存中直接解码、PoW 算力求解、官方异步文件上传与多模态模型会话路由
"""

import re
import io
import json
import time
import base64
import hashlib
import asyncio
import logging
import mimetypes
from typing import Dict, Any, List, Optional, Tuple
from curl_cffi import requests as cffi_requests
from curl_cffi.curl import CurlMime

from app.core.pow_solver import DeepSeekPOW
from app.engine.deepseek_client import (
    DeepSeekClient,
    DeepSeekAuthError,
    _AUTH_FAIL_CODES,
    _AUTH_FAIL_HINTS,
    extract_quota_reset_until,
)

logger = logging.getLogger(__name__)


class UnsupportedFileTypeError(Exception):
    """客户端上传的文件类型不受 DeepSeek 支持（如二进制 payload、非标准图片等）。属于客户端请求错误，不应熔断账号。"""
    pass


class DeepSeekMutedError(Exception):
    """账号被官方明确禁言（biz_code: 14 / 50006 或 biz_msg: user is muted）。属于账号级处罚，需隔离并记录解禁时间。"""
    def __init__(self, message: str, mute_until: Optional[float] = None):
        super().__init__(message)
        self.mute_until = mute_until


class DeepSeekQuotaExceededError(Exception):
    """账号上传/使用额度已耗尽（UploadFileErrorCode.USAGE_QUOTA_EXCEEDED = 16）。属于可恢复额度冷却，不应误判永久死号。"""
    def __init__(self, message: str, reset_at: Optional[int] = None, biz_code: Optional[int] = None):
        super().__init__(message)
        self.reset_at = reset_at
        self.biz_code = biz_code


class DeepSeekUploadRateLimitError(Exception):
    """上传接口触发 RPD/并发限流或风控挑战（7/8/15/40029/40303/40304）。需短暂冷却并换号容灾。"""
    def __init__(self, message: str, biz_code: Optional[int] = None, retry_after: int = 30):
        super().__init__(message)
        self.biz_code = biz_code
        self.retry_after = retry_after


class VisionPipeline:
    # 内存缓存，避免多轮对话中重复上传相同的图片: {cache_key: (file_id, ts)}
    # 注意（T5-A）：这是**无上限**的类级字典，旧实现永不清理，
    # 长期运行会随图片数量单调增长。此处加上容量与 TTL 双重淘汰。
    _file_cache: Dict[str, Tuple[str, float]] = {}
    _FILE_CACHE_TTL = 3600.0          # 与使用处一致：1 小时
    _FILE_CACHE_MAX_ENTRIES = 512     # 超出后淘汰最旧条目

    # ── 超时预算（T5-A） ──────────────────────────────────────────────
    # 实测依据：客户端看门狗约 **50 秒**。旧实现各步骤超时是各自写死的
    # （PoW 30s + 上传 30s + 就绪轮询 45s），最坏情况累计 **105 秒** ——
    # 远超看门狗，表现为"卡住几十秒然后报错"（TRANSPORT）。
    # 而且多张图片是**串行**上传的，5 张图最坏 225 秒，必然被掐断。
    # 现在改为：一次请求共享一个总预算，各步骤只能花剩余额度。
    VISION_TOTAL_BUDGET_SECONDS = 40.0   # 给看门狗留 ~10s 余量
    VISION_MIN_STEP_SECONDS = 3.0        # 单步最小额度，低于此值直接放弃并如实报错
    VISION_UPLOAD_CONCURRENCY = 2        # 图片并发上传数（与账号并发能力对齐，保守取值）

    # 图片体积与数量上限：旧实现完全不设限，一张 50MB 的图会被整体读进内存再上传
    VISION_MAX_IMAGE_BYTES = 12 * 1024 * 1024
    VISION_MAX_IMAGES = 8
    VISION_REMOTE_FETCH_TIMEOUT = 10.0   # 下载远程图片的单次超时

    @classmethod
    def _safe_json(cls, resp, context: str) -> Optional[Any]:
        """安全解析响应 JSON。

        T5-A：线上日志出现过
          `[OpenAI-Routes] Image upload failed: 'NoneType' object has no attribute 'get'`
        根因就是这里旧实现直接 `resp.json()` 后再链式 `.get(...)`：
        当上游返回非 JSON（HTML 错误页 / 空体 / CDN 拦截）时，`.json()` 会抛异常
        或被上层吞成 None，随后 `.get()` 就崩，而且错误信息完全指不到真正原因。
        这里统一成"解析失败 → 返回 None + 打印可诊断信息"。
        """
        try:
            return resp.json()
        except Exception as e:
            body = (getattr(resp, "text", "") or "")[:200].replace("\n", " ")
            logger.warning(
                f"[VisionPipeline] {context}: 响应非 JSON (HTTP {getattr(resp, 'status_code', '?')}, "
                f"{type(e).__name__}) body={body!r}"
            )
            return None

    @classmethod
    async def extract_images_and_text_async(
        cls, content: Any, extra_images: Optional[List[Any]] = None
    ) -> Tuple[str, List[Tuple[bytes, str, str]]]:
        """`extract_images_and_text` 的**异步安全**版本。

        ⚠️ 为什么不直接在端点里调同步版：
        `_resolve_image_data` 对 http(s) 图片会做**阻塞下载**（curl_cffi，单次最长
        15 秒）。而提取函数是在 async 端点里**同步**调用的 —— 于是一个带远程图片
        URL 的请求会把整个事件循环卡住最多 15 秒 ×图片数，
        期间**其他所有请求全部冻结**（不是变慢，是完全不响应）。
        这是图片通道最严重的可用性缺陷，比崩溃更隐蔽：网关看起来"活着"，
        但所有人都在等一个人的图片下载。

        放进线程池后阻塞只影响该请求自己。
        """
        return await asyncio.to_thread(cls.extract_images_and_text, content, extra_images)

    @classmethod
    def _cache_put(cls, cache_key: str, file_id: str) -> None:
        """写入缓存并做 TTL + 容量淘汰（防止无上限增长）"""
        now = time.time()
        cls._file_cache[cache_key] = (file_id, now)
        # 先按 TTL 清理
        expired = [k for k, (_, ts) in cls._file_cache.items() if now - ts >= cls._FILE_CACHE_TTL]
        for k in expired:
            cls._file_cache.pop(k, None)
        # 再按容量淘汰最旧
        if len(cls._file_cache) > cls._FILE_CACHE_MAX_ENTRIES:
            ordered = sorted(cls._file_cache.items(), key=lambda kv: kv[1][1])
            for k, _ in ordered[: len(cls._file_cache) - cls._FILE_CACHE_MAX_ENTRIES]:
                cls._file_cache.pop(k, None)

    @classmethod
    def _cache_get(cls, cache_key: str) -> Optional[str]:
        """读缓存（带 TTL 校验）"""
        cached = cls._file_cache.get(cache_key)
        if cached and (time.time() - cached[1] < cls._FILE_CACHE_TTL):
            return cached[0]
        return None

    @classmethod
    def invalidate_account_cache(cls, token: str) -> int:
        """账号失效时清掉它的缓存条目。

        file_id 是**账号级**的：账号被熔断/换号后，旧 file_id 在别的账号上引用
        会直接 `invalid ref file id`。缓存 key 里带 token 后缀本来就能隔离，
        但账号被换掉后那些条目永远不会再被命中，纯粹占内存 —— 这里显式清掉。
        """
        suffix = f"{token[-16:]}:"
        doomed = [k for k in cls._file_cache if k.startswith(suffix)]
        for k in doomed:
            cls._file_cache.pop(k, None)
        return len(doomed)

    #: 图片在**文本**里的占位符 —— 会话树里唯一那句"这里有一张图"。
    #:
    #: 为什么必须有它（实测缺口）：结构化图片块（OpenAI `image_url` / Claude `image` /
    #: Gemini `inline_data`）旧实现**只把图片塞进 `images`，从不往 `text_parts` 加任何东西**，
    #: 而末行是 `prompt = " ".join(text_parts).strip()` —— 于是纯图片消息产出**空串**，
    #: 归一化后就是 `content: ""`。
    #: 后果：**那一轮在会话树里完全没有痕迹**。图片只是外挂了一个 `file_id` 引用，
    #: 模态并没有"融合"进会话 —— 历史轮里"用户当时发了张图"这件事表达不出来。
    #:
    #: 必须**字节稳定**（同输入 → 同字节），否则会话指纹会漂、前缀匹配会失效。
    IMAGE_PLACEHOLDER = "[图片]"

    @classmethod
    def image_placeholder(cls, alt: Optional[str] = None) -> str:
        """图片在文本里的**唯一**表达方式。所有分支都必须走这里。

        为什么必须集中到一个函数：这里原先**两处各写一套** ——
        markdown 分支产 `[alt]`、raw data URI 分支产 `[图片]`，**格式已经不一致了**；
        再加上结构化块一处都不产，同一个"这一轮有图"在三类输入下**有三种表现**。
        （同一类漂移刚在免责声明上出过一次：两份文案清单，短变体在一条路径上漏清。）

        `alt` 为空时退化成 :data:`IMAGE_PLACEHOLDER`，保证不会产出 `[图片：]` 这种残缺串。
        """
        label = (alt or "").strip()
        return f"[图片：{label}]" if label else cls.IMAGE_PLACEHOLDER

    @classmethod
    def extract_images_and_text(
        cls, content: Any, extra_images: Optional[List[Any]] = None
    ) -> Tuple[str, List[Tuple[bytes, str, str]]]:
        """
        从各种常见 Agent (OpenAI / Claude / Gemini / Ollama / Open WebUI 等) 消息体中
        提取文本提示与图片二进制数据。
        返回: (prompt_text, [(image_bytes, filename, mime_type), ...])
        """
        text_parts = []
        images = []

        # 0. 先处理顶层 extra_images (常见于 Ollama, Open WebUI, Dify, Chatbox 等客户端)
        #    同样要写文本占位符 —— 否则这类客户端发来的纯图片消息会让 prompt 变成空串
        if extra_images and isinstance(extra_images, list):
            for extra in extra_images:
                if isinstance(extra, str):
                    img_tuple = cls._resolve_image_data(extra)
                    if img_tuple:
                        images.append(img_tuple)
                        text_parts.append(cls.image_placeholder())
                    else:
                        # 尝试纯 base64 字符串
                        try:
                            raw_b = base64.b64decode(extra)
                            fmt = cls.detect_raster_format(raw_b)
                            if fmt:
                                ext, mime_type = fmt
                                fname = f"extra_{int(time.time())}.{ext}"
                                images.append((raw_b, fname, mime_type))
                                text_parts.append(cls.image_placeholder())
                        except Exception:
                            pass
                elif isinstance(extra, dict):
                    url = extra.get("url") or extra.get("image_url") or extra.get("data")
                    if url:
                        img_tuple = cls._resolve_image_data(str(url))
                        if img_tuple:
                            images.append(img_tuple)
                            text_parts.append(cls.image_placeholder())

        # 1. 纯文本字符串：检查是否内嵌了 Markdown 图片或 Data URI
        if isinstance(content, str):
            # 检查是否有 markdown 形式的 data URI 或 http 图片: ![alt](data:image/...;base64,...)
            def _sub_md_img(match):
                alt = match.group(1)
                src = match.group(2)
                img_tuple = cls._resolve_image_data(src)
                if img_tuple:
                    images.append(img_tuple)
                    return cls.image_placeholder(alt)
                return match.group(0)

            cleaned_str = re.sub(
                r"!\[(.*?)\]\((data:image\/[a-zA-Z0-9\+\-]+;base64,[A-Za-z0-9+/=]+|https?://[^\s\)]+)\)",
                _sub_md_img,
                content,
            )

            # 检查是否有裸露在文本里的 data URI
            def _sub_raw_data_uri(match):
                mime = match.group(1)
                b64 = match.group(2)
                try:
                    raw_bytes = base64.b64decode(b64)
                    fmt = cls.detect_raster_format(raw_bytes)
                    if fmt:
                        ext, clean_mime = fmt
                        fname = f"inline_{int(time.time())}.{ext}"
                        images.append((raw_bytes, fname, clean_mime))
                        return cls.image_placeholder()
                    return match.group(0)
                except Exception:
                    return match.group(0)

            cleaned_str = re.sub(
                r"data:(image\/[a-zA-Z0-9\+\-]+);base64,([A-Za-z0-9+/=]+)",
                _sub_raw_data_uri,
                cleaned_str,
            )
            # ★ 必须把第 0 分支（顶层 extra_images）已经写进 text_parts 的占位符带上。
            # 旧实现直接 `return cleaned_str.strip(), images` —— **整个丢弃 text_parts**，
            # 于是"文本 + 顶层 images 数组"这种客户端（Ollama / Open WebUI / Dify / Chatbox）
            # 发来的图，在文本里一个痕迹都不留。
            # 这个漏失由 tests/test_vision_robustness.py 的
            # `test_top_level_extra_images_is_not_empty` 抓出。
            joined = " ".join([p for p in text_parts if p] + [cleaned_str.strip()]).strip()
            if len(images) > cls.VISION_MAX_IMAGES:
                logger.warning(
                    f"[Vision] 文本内嵌图片含 {len(images)} 张，超过上限 {cls.VISION_MAX_IMAGES}，"
                    f"只处理前 {cls.VISION_MAX_IMAGES} 张"
                )
                images = images[: cls.VISION_MAX_IMAGES]
            return joined, images

        # 2. 结构化块列表 (OpenAI / Claude / Gemini 多模态)
        if isinstance(content, list):
            unknown_types: List[str] = []
            for part in content:
                if not isinstance(part, dict):
                    unknown_types.append(f"<{type(part).__name__}>")
                    continue
                part_type = str(part.get("type", "")).lower()

                # (a) 纯文本
                if part_type == "text":
                    t = part.get("text", "")
                    if t:
                        text_parts.append(t)

                # (b) OpenAI 格式 image_url: {"type": "image_url", "image_url": {"url": "..."}}
                elif part_type == "image_url":
                    img_info = part.get("image_url", {})
                    url = img_info.get("url", "") if isinstance(img_info, dict) else str(img_info)
                    img_tuple = cls._resolve_image_data(url)
                    if img_tuple:
                        images.append(img_tuple)
                        # ★ 必须同时写进文本：只塞 images 会让这一轮的 prompt 变成空串
                        text_parts.append(cls.image_placeholder())
                    else:
                        logger.warning(
                            f"[Vision] image_url 块解析不出图片（已跳过）: {str(url)[:120]!r}"
                        )

                # (c) Claude / 通用 image 格式
                elif part_type in ("image", "input_image"):
                    source = part.get("source")
                    if isinstance(source, dict) and source.get("type") == "base64":
                        data_b64 = source.get("data", "")
                        media_type = source.get("media_type", "image/png")
                        try:
                            raw_bytes = base64.b64decode(data_b64)
                            fmt = cls.detect_raster_format(raw_bytes)
                            if fmt:
                                ext, clean_mime = fmt
                                fname = f"upload_{int(time.time())}.{ext}"
                                images.append((raw_bytes, fname, clean_mime))
                                text_parts.append(cls.image_placeholder())
                            else:
                                logger.info(f"[Vision] Claude source 非有效光栅图片格式 (media_type={media_type})，已跳过")
                        except Exception as e:
                            logger.warning(f"[Vision] Claude source Base64 解码失败: {e}")
                    else:
                        # 兼容直接在 part 下传 url / data / image_url
                        url = part.get("url") or part.get("image_url") or part.get("data")
                        if url:
                            img_tuple = cls._resolve_image_data(str(url))
                            if img_tuple:
                                images.append(img_tuple)
                                text_parts.append(cls.image_placeholder())
                            else:
                                logger.warning(
                                    f"[Vision] image 块解析不出图片（已跳过）: {str(url)[:120]!r}"
                                )

                # (d) Google Gemini 格式: {"inline_data": {"mime_type": "...", "data": "..."}}
                elif "inline_data" in part or "inlineData" in part:
                    idata = part.get("inline_data") or part.get("inlineData") or {}
                    m_type = idata.get("mime_type") or idata.get("mimeType", "image/png")
                    d_b64 = idata.get("data", "")
                    try:
                        raw_bytes = base64.b64decode(d_b64)
                        fmt = cls.detect_raster_format(raw_bytes)
                        if fmt:
                            ext, clean_mime = fmt
                            fname = f"gemini_{int(time.time())}.{ext}"
                            images.append((raw_bytes, fname, clean_mime))
                            text_parts.append(cls.image_placeholder())
                        else:
                            logger.info(f"[Vision] Gemini inline_data 非有效光栅图片格式 (mime={m_type})，已跳过")
                    except Exception as e:
                        logger.warning(f"[Vision] Gemini inline_data Base64 解码失败: {e}")

                else:
                    unknown_types.append(part_type or "<无 type 字段>")

            # T5-A：旧实现对未知 type **完全静默跳过**，既不记日志也不报错。
            # 后果：客户端换了个新格式（例如 Responses API 的 input_image 变体），
            # 用户看到的是"模型对我的图视而不见"，而网关这边一片安静 ——
            # 属于最难查的那类故障。
            if unknown_types:
                logger.warning(
                    f"[Vision] 结构化 content 里有 {len(unknown_types)} 个未识别的块，已跳过: "
                    f"{sorted(set(unknown_types))[:8]}"
                    f"（若用户反馈「图片没被看到」，请对照此处排查客户端格式）"
                )
            if not text_parts and not images:
                try:
                    shape = [
                        {k: (type(v).__name__) for k, v in p.items()}
                        for p in content[:3] if isinstance(p, dict)
                    ]
                except Exception:
                    shape = []
                logger.warning(
                    f"[Vision] 结构化 content 既没解析出文本也没解析出图片 "
                    f"(共 {len(content)} 个块) 形状={shape}"
                )

        prompt = " ".join(text_parts).strip()
        # 数量闸门：超出的图片直接丢弃并明确告知，不静默截断
        if len(images) > cls.VISION_MAX_IMAGES:
            logger.warning(
                f"[Vision] 本次请求含 {len(images)} 张图片，超过上限 {cls.VISION_MAX_IMAGES}，"
                f"只处理前 {cls.VISION_MAX_IMAGES} 张"
            )
            images = images[: cls.VISION_MAX_IMAGES]
        return prompt, images

    SUPPORTED_IMAGE_MIMES = {
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/webp",
    }

    @classmethod
    def detect_raster_format(cls, content: bytes) -> Optional[Tuple[str, str]]:
        """根据魔法字节严格校验并识别 DeepSeek 官方接口支持的光栅图片格式。

        返回: (ext, mime_type) 或 None (非光栅图片，如矢量 SVG、ICO、HTML、文本等)。
        """
        if not content or len(content) < 8:
            return None
        # PNG: \x89PNG\r\n\x1a\n
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png", "image/png"
        # JPEG: \xff\xd8\xff
        if content.startswith(b"\xff\xd8\xff"):
            return "jpg", "image/jpeg"
        # WEBP: RIFF....WEBP
        if content.startswith(b"RIFF") and len(content) >= 12 and content[8:12] == b"WEBP":
            return "webp", "image/webp"
        return None

    @classmethod
    def _resolve_image_data(cls, url: str) -> Optional[Tuple[bytes, str, str]]:
        """将 data URI 或 http URL 转换为 (raw_bytes, filename, mime_type)"""
        if not url:
            return None

        # Data URI 格式: data:image/png;base64,xxxx
        if url.startswith("data:"):
            match = re.match(r"^data:(image\/[a-zA-Z0-9\+\-]+);base64,(.+)$", url)
            if match:
                declared_mime = match.group(1).lower()
                b64_str = match.group(2)
                try:
                    raw_bytes = base64.b64decode(b64_str)
                    fmt = cls.detect_raster_format(raw_bytes)
                    if not fmt:
                        logger.info(f"[Vision] 忽略非光栅或不受支持的 Data URI 格式 (mime={declared_mime})")
                        return None
                    ext, mime_type = fmt
                    fname = f"image_{int(time.time())}.{ext}"
                    return raw_bytes, fname, mime_type
                except Exception as e:
                    print(f"[Vision] Data URI 解析失败: {e}")
                    return None

        # 网络图片 URL
        if url.startswith("http://") or url.startswith("https://"):
            # 扩展名快速前置拦截：明显非光栅图片（svg, ico, xml, html等）直接跳过网络请求
            clean_url = url.split("?")[0].lower()
            if clean_url.endswith((".svg", ".ico", ".xml", ".html", ".htm", ".txt", ".json")):
                logger.info(f"[Vision] 忽略非光栅图片 URL (扩展名不符): url={url[:120]}")
                return None

            try:
                r = cffi_requests.get(url, timeout=cls.VISION_REMOTE_FETCH_TIMEOUT)
                if r.status_code == 200:
                    content = r.content or b""
                    # T5-A：旧实现完全不管体积，一张大图会被整体读进内存再上传
                    if len(content) > cls.VISION_MAX_IMAGE_BYTES:
                        logger.warning(
                            f"[Vision] 远程图片过大，已跳过: {len(content):,} 字节 "
                            f"(上限 {cls.VISION_MAX_IMAGE_BYTES:,}) url={url[:120]}"
                        )
                        return None
                    if not content:
                        logger.warning(f"[Vision] 远程图片内容为空，已跳过: url={url[:120]}")
                        return None

                    # 严格依据内容魔法字节校验光栅图片格式，杜绝 SVG 徽章、HTML 报错页打崩上游
                    fmt = cls.detect_raster_format(content)
                    if not fmt:
                        logger.info(
                            f"[Vision] 远程 URL 返回的内容非标准光栅图片 (Content-Type: {r.headers.get('content-type')!r})，已跳过: "
                            f"url={url[:120]}"
                        )
                        return None

                    ext, mime = fmt
                    fname = f"remote_{int(time.time())}.{ext}"
                    return content, fname, mime
                logger.warning(
                    f"[Vision] 远程图片下载失败 HTTP {r.status_code}: url={url[:120]}"
                )
            except Exception as e:
                logger.warning(f"[Vision] 下载外部图片失败 ({type(e).__name__}): {e} url={url[:120]}")
            return None

        return None

    @classmethod
    def _remaining(cls, deadline: float, step: str) -> float:
        """取本次请求剩余的超时预算；不足则如实报错。

        T5-A：旧实现各步骤超时写死（30/30/45s），最坏累计 105 秒，
        而客户端看门狗约 50 秒 —— 客户端早就放弃了，网关还在埋头干活，
        用户看到的是"卡住几十秒然后报错"。改成统一预算后，网关**在客户端放弃之前**
        就自己收手并给出明确错误，至少日志里能看清是谁慢。
        """
        left = deadline - time.monotonic()
        if left < cls.VISION_MIN_STEP_SECONDS:
            raise TimeoutError(
                f"图片通道总预算（{cls.VISION_TOTAL_BUDGET_SECONDS:.0f}s）已在「{step}」处耗尽，"
                f"主动放弃以避免客户端看门狗先超时"
            )
        return left

    @classmethod
    async def upload_images(
        cls,
        token: str,
        images: List[Tuple[bytes, str, str]],
        cookie: str = "",
        proxy: Optional[str] = None,
        deadline: Optional[float] = None,
    ) -> List[str]:
        """**有界并发**上传多张图片，共享一个总预算。

        旧实现是串行 for 循环：5 张图各 45 秒 → 最坏 225 秒，必然被看门狗掐断。
        并发度取 2：与账号已知的并发舒适区一致（实测单账号 2 条生成流），
        上传走的是另一个端点，保守取值比激进更安全。
        """
        if not images:
            return []
        if deadline is None:
            calc_budget = max(cls.VISION_TOTAL_BUDGET_SECONDS, min(120.0, 30.0 + len(images) * 8.0))
            deadline = time.monotonic() + calc_budget

        # 同内容图片先去重再上传。
        # 实测依据（2026-09-11 生产）：一次请求里 3 张**完全相同**的图，
        # 日志里出现了 **2 个** file_id —— 并发度 2 让两张同时在缓存命中之前
        # 各传了一遍（典型的缓存击穿）。上传是真金白银的时间，去重是白捡的。
        dedup: Dict[str, int] = {}
        unique: List[Tuple[bytes, str, str]] = []
        index_map: List[int] = []
        for item in images:
            h = hashlib.sha256(item[0]).hexdigest()
            if h in dedup:
                index_map.append(dedup[h])
            else:
                dedup[h] = len(unique)
                index_map.append(len(unique))
                unique.append(item)

        sem = asyncio.Semaphore(cls.VISION_UPLOAD_CONCURRENCY)

        async def _one(idx: int, item: Tuple[bytes, str, str]) -> str:
            img_bytes, fname, mtype = item
            if len(img_bytes) > cls.VISION_MAX_IMAGE_BYTES:
                raise ValueError(
                    f"第 {idx + 1} 张图片过大（{len(img_bytes):,} 字节 > 上限 "
                    f"{cls.VISION_MAX_IMAGE_BYTES:,}），拒绝上传"
                )
            async with sem:
                return await cls.upload_image_to_deepseek(
                    token=token, image_bytes=img_bytes, filename=fname, mime_type=mtype,
                    cookie=cookie, proxy=proxy, deadline=deadline,
                )

        results = await asyncio.gather(
            *[_one(i, item) for i, item in enumerate(unique)], return_exceptions=True
        )
        ok: List[str] = []
        errors: List[str] = []
        for i, r in enumerate(results):
            if isinstance(r, DeepSeekAuthError):
                # 账号凭证失效直接向上暴露，由路由层接管做自动重登或换号容灾
                raise r
            elif isinstance(r, DeepSeekMutedError):
                # 账号被官方明确禁言，直接向上暴露，由路由层做精准账号隔离
                raise r
            elif isinstance(r, (DeepSeekQuotaExceededError, DeepSeekUploadRateLimitError)):
                # 账号额度耗尽 / 并发限流 / 风控挑战，直接向上暴露，由路由层做冷却与换号容灾
                raise r
            elif isinstance(r, UnsupportedFileTypeError):
                # 客户端文件类型不受支持，直接向上暴露，避免被当成账号故障误熔断
                raise r
            elif isinstance(r, (TimeoutError, asyncio.TimeoutError)):
                # 超时异常向上暴露，由路由层做无害换号，严禁误判为封号
                raise r
            elif isinstance(r, Exception):
                errors.append(f"第 {i + 1} 张: {type(r).__name__}: {r}")
            else:
                ok.append(r)
        if errors:
            # 只要有失败就抛出，并附上成功的数量 —— 静默丢掉一张图会让用户
            # 以为模型"没看懂"，而实际是上传失败
            raise RuntimeError(
                f"{len(errors)}/{len(unique)} 张图片上传失败（成功 {len(ok)} 张）: "
                + "；".join(errors[:3])
            )
        if len(unique) < len(images):
            logger.info(
                f"[VisionPipeline] 本次 {len(images)} 张图片中 {len(images) - len(unique)} 张内容重复，已去重"
            )
        return [ok[i] for i in index_map]

    @classmethod
    def _raise_on_biz_error(
        cls,
        stage: str,
        code: Any,
        msg: str,
        biz_code: Any,
        biz_msg: str,
        biz_data: Dict[str, Any],
    ) -> None:
        """统一判别 PoW/上传接口返回的新版业务与全局错误码（Web 2.8.0）。"""
        msg_lower = (msg or "").lower()
        biz_lower = (biz_msg or "").lower()
        bd = biz_data if isinstance(biz_data, dict) else {}

        if (
            (isinstance(code, int) and code in _AUTH_FAIL_CODES)
            or any(h in msg_lower for h in _AUTH_FAIL_HINTS)
            or any(h in biz_lower for h in _AUTH_FAIL_HINTS)
        ):
            raise DeepSeekAuthError(
                f"{stage}被拒 (凭证失效): code={code} msg={msg!r} biz_code={biz_code} biz_msg={biz_msg!r}"
            )

        if (
            code == 50006
            or biz_code in (14, 50006)
            or "user is muted" in biz_lower
            or "user is muted" in msg_lower
            or (isinstance(bd.get("mute"), dict))
        ):
            parsed_until = extract_quota_reset_until(bd)
            raise DeepSeekMutedError(
                f"{stage}被官方禁言 (biz_code={biz_code}, msg={biz_msg or msg!r})",
                mute_until=parsed_until,
            )

        quotas = bd.get("quotas") if isinstance(bd.get("quotas"), list) else []
        if (
            biz_code == 16
            or "usage_quota_exceeded" in biz_lower
            or "usage_quota_exceeded" in msg_lower
            or any(isinstance(q, dict) and q.get("is_exhausted") for q in quotas)
        ):
            reset_at = extract_quota_reset_until(bd)
            raise DeepSeekQuotaExceededError(
                f"{stage}触发使用额度上限 (biz_code={biz_code}, msg={biz_msg or msg!r})",
                reset_at=reset_at,
                biz_code=biz_code if isinstance(biz_code, int) else None,
            )

        if (
            biz_code in (7, 8, 15, 40029, 40303, 40304)
            or code in (429, 40029, 40303, 40304)
            or "challenge_required" in biz_lower
            or "rate_limit" in biz_lower
        ):
            retry_after = 120 if (biz_code in (40303, 40304) or code in (40303, 40304)) else 30
            raise DeepSeekUploadRateLimitError(
                f"{stage}触发上游限流或挑战 (code={code}, biz_code={biz_code}, msg={biz_msg or msg!r})",
                biz_code=biz_code if isinstance(biz_code, int) else None,
                retry_after=retry_after,
            )

        if biz_code == 9 or "unsupported file type" in biz_lower or "unsupported file type" in msg_lower:
            raise UnsupportedFileTypeError(
                f"文件格式不受 DeepSeek 识图接口支持 (biz_code={biz_code}, msg={biz_msg or msg!r})"
            )

    @classmethod
    async def upload_image_to_deepseek(
        cls,
        token: str,
        image_bytes: bytes,
        filename: str = "image.png",
        mime_type: str = "image/png",
        cookie: str = "",
        proxy: Optional[str] = None,
        timeout: Optional[float] = None,
        deadline: Optional[float] = None,
    ) -> str:
        """
        在内存中将图片直接上传至 DeepSeek 官方文件服务，返回合法的 file_id。
        完全基于内存流，不写磁盘，零本地临时文件。

        :param deadline: 绝对截止时间（`time.monotonic()` 基准）。传入时**所有步骤
            共享同一个预算**；不传则按 `VISION_TOTAL_BUDGET_SECONDS` 现算一个。
        :param timeout: 兼容旧签名 —— 旧的"就绪轮询超时"，现在只在未给 deadline 时用于
            推算预算。
        """
        if len(image_bytes) > cls.VISION_MAX_IMAGE_BYTES:
            raise ValueError(
                f"图片过大（{len(image_bytes):,} 字节 > 上限 {cls.VISION_MAX_IMAGE_BYTES:,}）"
            )
        if deadline is None:
            budget = min(timeout, cls.VISION_TOTAL_BUDGET_SECONDS) if timeout else cls.VISION_TOTAL_BUDGET_SECONDS
            deadline = time.monotonic() + budget

        # 1. 查缓存（按账号 Token 强隔离，避免跨账号文件 ID 污染引发 invalid ref file id）
        sha = hashlib.sha256(image_bytes).hexdigest()
        cache_key = f"{token[-16:]}:{sha}"
        cached_fid = cls._cache_get(cache_key)
        if cached_fid:
            logger.info(f"[VisionPipeline] Image cache hit: sha={sha[:8]}, fid={cached_fid}")
            return cached_fid

        logger.info(f"[VisionPipeline] Uploading image {filename} ({len(image_bytes)} bytes)...")

        from app.engine.proxy_manager import execute_with_proxy_failover

        # 2. 求解 PoW
        plat = DeepSeekClient.get_client_platform()
        impersonate = DeepSeekClient.get_impersonate(plat)
        headers = DeepSeekClient.build_headers(token, cookie, platform=plat)

        def _get_pow():
            r = execute_with_proxy_failover(
                lambda p_url: cffi_requests.post(
                    "https://chat.deepseek.com/api/v0/chat/create_pow_challenge",
                    json={"target_path": "/api/v0/file/upload_file"},
                    headers=headers,
                    impersonate=impersonate,
                    timeout=cls._remaining(deadline, "获取 PoW 挑战"),
                    proxies={"http": p_url, "https": p_url} if p_url else None,
                ),
                primary_proxy=proxy,
                context="上传 PoW 挑战",
            )
            data = cls._safe_json(r, "获取上传 PoW 挑战")
            if data is None:
                raise RuntimeError(
                    f"PoW 挑战接口返回非 JSON (HTTP {r.status_code}): {(r.text or '')[:150]!r}"
                )
            code = data.get("code") if isinstance(data, dict) else None
            msg = str(data.get("msg") or "") if isinstance(data, dict) else ""
            data_obj = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else {}
            biz_code = data_obj.get("biz_code")
            biz_msg = str(data_obj.get("biz_msg") or "")

            biz_data = data_obj.get("biz_data") if isinstance(data_obj.get("biz_data"), dict) else {}
            cls._raise_on_biz_error("获取上传 PoW 挑战", code, msg, biz_code, biz_msg, biz_data)
            ch = biz_data.get("challenge") if isinstance(biz_data, dict) else None

            if not ch:
                raise RuntimeError(
                    f"获取上传 PoW 挑战失败 (code={code}, msg={msg!r}, biz_code={biz_code}, biz_msg={biz_msg!r}): "
                    f"{json.dumps(data, ensure_ascii=False)[:200]}"
                )
            return DeepSeekPOW.solve(ch)

        rem_pow = cls._remaining(deadline, "求解 PoW")
        pow_token = await asyncio.wait_for(
            asyncio.to_thread(_get_pow),
            timeout=rem_pow,
        )

        # 3. Multipart 内存直传
        upload_headers = DeepSeekClient.build_headers(
            token, cookie, platform=plat, include_content_type=False
        )
        upload_headers.update({
            "x-thinking-enabled": "true",
            "x-model-type": "vision",
            "x-file-size": str(len(image_bytes)),
            "x-ds-pow-response": pow_token,
        })

        def _upload():
            t_u0 = time.time()
            def _do_upload(p_url):
                mime = CurlMime()
                mime.addpart(name="file", data=image_bytes, filename=filename, content_type=mime_type)
                return cffi_requests.post(
                    "https://chat.deepseek.com/api/v0/file/upload_file",
                    headers=upload_headers,
                    multipart=mime,
                    impersonate=impersonate,
                    timeout=cls._remaining(deadline, "上传图片"),
                    proxies={"http": p_url, "https": p_url} if p_url else None,
                )

            resp = execute_with_proxy_failover(
                _do_upload,
                primary_proxy=proxy,
                context="上传图片",
            )
            d = cls._safe_json(resp, "上传图片")
            try:
                from app.engine.drop_probe import record_event
                d_obj = (d or {}).get("data") if isinstance(d, dict) and isinstance(d.get("data"), dict) else {}
                record_event(
                    "upload_file",
                    token=token,
                    platform=plat,
                    impersonate=impersonate,
                    has_cookie=bool(cookie),
                    proxy_primary=proxy,
                    http_status=resp.status_code,
                    code=(d or {}).get("code") if isinstance(d, dict) else None,
                    biz_code=d_obj.get("biz_code"),
                    msg=str(d_obj.get("biz_msg") or (d or {}).get("msg") or "") if isinstance(d, dict) else "",
                    latency_ms=(time.time() - t_u0) * 1000,
                )
            except Exception:
                pass
            if d is None:
                raise RuntimeError(
                    f"图片上传返回非 JSON (HTTP {resp.status_code}): {(resp.text or '')[:150]!r}"
                )
            code = d.get("code") if isinstance(d, dict) else None
            msg = str(d.get("msg") or "") if isinstance(d, dict) else ""
            data_obj = d.get("data") if isinstance(d, dict) and isinstance(d.get("data"), dict) else {}
            biz_code = data_obj.get("biz_code")
            biz_msg = str(data_obj.get("biz_msg") or "")
            biz_data = data_obj.get("biz_data") if isinstance(data_obj.get("biz_data"), dict) else {}
            cls._raise_on_biz_error("上传图片", code, msg, biz_code, biz_msg, biz_data)

            fid = biz_data.get("id") if isinstance(biz_data, dict) else None
            if not fid:
                raise RuntimeError(
                    f"图片上传失败 (code={code}, msg={msg!r}, biz_code={biz_code}, biz_msg={biz_msg!r}): "
                    f"{json.dumps(d, ensure_ascii=False)[:200]}"
                )
            return fid

        rem_upload = cls._remaining(deadline, "上传图片")
        file_id = await asyncio.wait_for(
            asyncio.to_thread(_upload),
            timeout=rem_upload,
        )
        logger.info(f"[VisionPipeline] Image uploaded, fid={file_id}, waiting for official processing...")

        # 4. 轮询状态直至 SUCCESS（共享同一个总预算，不再是自己一个 45s）
        last_status = "UNKNOWN"
        consecutive_errors = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.6)

            def _poll():
                r = execute_with_proxy_failover(
                    lambda p_url: cffi_requests.get(
                        "https://chat.deepseek.com/api/v0/file/fetch_files",
                        params={"file_ids": file_id},
                        headers=headers,
                        impersonate=impersonate,
                        timeout=min(30, max(3, deadline - time.monotonic())),
                        proxies={"http": p_url, "https": p_url} if p_url else None,
                    ),
                    primary_proxy=proxy,
                    context="轮询文件状态",
                )
                # T5-A：旧实现 `r.json().get(...)` 在非 JSON 响应时直接崩
                # （线上日志的 'NoneType' object has no attribute 'get' 即此处）
                d = cls._safe_json(r, "轮询文件状态")
                if d is None:
                    return "ERROR_NONJSON"
                code = d.get("code") if isinstance(d, dict) else None
                if isinstance(code, int) and code in _AUTH_FAIL_CODES:
                    raise DeepSeekAuthError(f"轮询文件状态凭证失效: code={code} (HTTP {r.status_code})")
                data_obj = d.get("data") if isinstance(d, dict) and isinstance(d.get("data"), dict) else {}
                biz_data = data_obj.get("biz_data") if isinstance(data_obj.get("biz_data"), dict) else {}
                flist = biz_data.get("files") if isinstance(biz_data.get("files"), list) else []
                if flist and isinstance(flist[0], dict):
                    return (flist[0].get("status") or "").upper()
                # 列表为空：可能是瞬时抖动，也可能是 file_id 根本不存在
                return "MISSING"

            status = await asyncio.to_thread(_poll)
            last_status = status

            if status == "SUCCESS":
                cls._cache_put(cache_key, file_id)
                logger.info(f"[VisionPipeline] Image {file_id} ready (status=SUCCESS)")
                return file_id
            if "FAIL" in status or "ERROR" in status:
                # MISSING 在上游语义里等同于"文件不存在/不可引用"，属于终态
                raise RuntimeError(
                    f"DeepSeek 官方服务端解析文件失败: status={status}, file_id={file_id}"
                )
            if status == "MISSING":
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    raise RuntimeError(
                        f"文件 {file_id} 连续 {consecutive_errors} 次查询不到（可能 file_id 无效或归属错误账号）。"
                        f"注意 file_id 是账号级的，换账号后必须重新上传。"
                    )
            else:
                consecutive_errors = 0

        raise TimeoutError(
            f"等待 DeepSeek 官方处理文件超时: file_id={file_id}, 最后状态={last_status}, "
            f"总预算 {cls.VISION_TOTAL_BUDGET_SECONDS:.0f}s 已用尽"
        )
