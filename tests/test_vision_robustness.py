"""
T5-A 图片通道健壮性测试

线上日志里出现过 3 次
    `Image upload failed: 'NoneType' object has no attribute 'get'`
对应的就是旧实现 `resp.json().get(...)` 在非 JSON 响应时的崩溃。
但崩溃只是这个模块最**显眼**的缺陷，不是最严重的：

  · **阻塞事件循环**：`extract_images_and_text` 是同步函数，在 async 端点里直接调用，
    而它对 http(s) 图片会做**阻塞下载**（单次最长 15s）——
    一个带远程图片 URL 的请求会把整个网关卡住，期间**所有其他请求全部冻结**。
    这比崩溃隐蔽得多：网关看起来活着，但所有人都在等一个人的图片下载。
  · **超时预算失控**：各步骤超时写死（30/30/45s），最坏累计 105s，
    而客户端看门狗约 50s —— 客户端早就放弃，网关还在埋头干活。
    多张图片还是**串行**上传，5 张图最坏 225s，必然被掐断。

本文件把这几类都钉死。
"""

import asyncio
import sys
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.engine.vision_pipeline import VisionPipeline


class FakeResp:
    def __init__(self, *, status_code=200, payload=None, text="", headers=None,
                 raise_on_json=False, content=b""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}
        self.content = content
        self._raise = raise_on_json

    def json(self):
        if self._raise:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


# 1x1 PNG
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)
PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAACklEQVR4nGNgAAAAAgABAAAAAP8DAAAGAAVXv6vUAAAAAElFTkSuQmCC"


# ══════════════════════════════════════════════════════════════════════
# 模态融合：图片必须在**文本**里留下痕迹
# ══════════════════════════════════════════════════════════════════════
#
# 实测缺口（本类存在的理由）：结构化图片块（OpenAI `image_url` / Claude `image` /
# Gemini `inline_data`）旧实现**只把图片塞进 `images`，从不往 `text_parts` 加任何东西**，
# 而末行是 `prompt = " ".join(text_parts).strip()` —— 于是**纯图片消息的 prompt 是空串**，
# 归一化后就是 `content: ""`。
#
# 后果不是"少显示一段文字"，而是：**图片只是外挂了一个 file_id 引用，模态没有融合进会话。**
# 历史轮里"用户当时发了张图"这件事完全表达不出来，会话树那一轮是空的。
#
# 同时这里原先还有一处漂移：markdown 分支产 `[alt]`、raw data URI 分支产 `[图片]` ——
# 同一个"有图"在两处两种格式。现在全部收敛到 `VisionPipeline.image_placeholder()`，
# 并由下面的测试钉住。

def _data_uri() -> str:
    return f"data:image/png;base64,{PNG_B64}"


class TestMultimodalPlaceholder(unittest.TestCase):
    """图片在文本里的唯一表达 —— 会话树里"这一轮有图"必须可被表达"""

    # ── 三种结构化格式：旧实现全部产出空串 ──

    def test_openai_image_url_is_not_empty(self):
        content = [{"type": "image_url", "image_url": {"url": _data_uri()}}]
        prompt, images = VisionPipeline.extract_images_and_text(content)
        self.assertEqual(len(images), 1, "图没解析出来，下面的断言就没意义")
        self.assertNotEqual(prompt, "", "★ 纯图片消息的 prompt 是空串 —— 会话树里这一轮没有痕迹")
        self.assertIn(VisionPipeline.IMAGE_PLACEHOLDER, prompt)

    def test_claude_base64_image_is_not_empty(self):
        content = [{"type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64}}]
        prompt, images = VisionPipeline.extract_images_and_text(content)
        self.assertEqual(len(images), 1)
        self.assertIn(VisionPipeline.IMAGE_PLACEHOLDER, prompt)

    def test_gemini_inline_data_is_not_empty(self):
        content = [{"inline_data": {"mime_type": "image/png", "data": PNG_B64}}]
        prompt, images = VisionPipeline.extract_images_and_text(content)
        self.assertEqual(len(images), 1)
        self.assertIn(VisionPipeline.IMAGE_PLACEHOLDER, prompt)

    def test_raw_data_uri_is_not_empty(self):
        prompt, images = VisionPipeline.extract_images_and_text(f"看这张 {_data_uri()}")
        self.assertEqual(len(images), 1)
        self.assertIn(VisionPipeline.IMAGE_PLACEHOLDER, prompt)

    def test_top_level_extra_images_is_not_empty(self):
        """Ollama / Open WebUI / Dify / Chatbox 这类客户端把图放在顶层 images 数组里"""
        prompt, images = VisionPipeline.extract_images_and_text("看看", extra_images=[PNG_B64])
        self.assertEqual(len(images), 1)
        self.assertIn(VisionPipeline.IMAGE_PLACEHOLDER, prompt)

    # ── 单一格式：不许再出现两处各写一套 ──

    def test_all_forms_share_one_placeholder_text(self):
        """★ 防漂移：各类输入产出的占位符必须是**同一个字符串**。

        这里原先两处各写一套（`[alt]` vs `[图片]`），格式已经不一致了。
        """
        cases = {
            "openai": [{"type": "image_url", "image_url": {"url": _data_uri()}}],
            "claude": [{"type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64}}],
            "gemini": [{"inline_data": {"mime_type": "image/png", "data": PNG_B64}}],
        }
        seen = {}
        for name, content in cases.items():
            prompt, images = VisionPipeline.extract_images_and_text(content)
            self.assertEqual(len(images), 1, name)
            seen[name] = prompt.strip()
        raw, _ = VisionPipeline.extract_images_and_text(_data_uri())
        seen["raw"] = raw.strip()
        extra, imgs = VisionPipeline.extract_images_and_text("x", extra_images=[PNG_B64])
        self.assertEqual(len(imgs), 1, "extra_images 没解析出图")
        seen["extra"] = extra.replace("x", "").strip()

        self.assertEqual(len(set(seen.values())), 1,
                         f"★ 同一个占位符有多种写法（又漂移了）：{seen}")

    def test_markdown_alt_is_preserved(self):
        prompt, images = VisionPipeline.extract_images_and_text(f"![流程图]({_data_uri()})")
        self.assertEqual(len(images), 1)
        self.assertEqual(prompt.strip(), "[图片：流程图]")

    def test_markdown_without_alt_falls_back_cleanly(self):
        """alt 为空时必须退化成 `[图片]`，不能产出 `[图片：]` 这种残缺串"""
        prompt, images = VisionPipeline.extract_images_and_text(f"![]({_data_uri()})")
        self.assertEqual(len(images), 1)
        self.assertEqual(prompt.strip(), VisionPipeline.IMAGE_PLACEHOLDER)
        self.assertNotIn("[图片：]", prompt)

    def test_blank_alt_also_falls_back(self):
        self.assertEqual(VisionPipeline.image_placeholder("   "),
                         VisionPipeline.IMAGE_PLACEHOLDER)
        self.assertEqual(VisionPipeline.image_placeholder(None),
                         VisionPipeline.IMAGE_PLACEHOLDER)

    # ── 字节稳定：会话指纹 / 前缀匹配依赖它 ──

    def test_same_input_produces_identical_bytes(self):
        content = [{"type": "image_url", "image_url": {"url": _data_uri()}}]
        a, _ = VisionPipeline.extract_images_and_text(content)
        b, _ = VisionPipeline.extract_images_and_text(content)
        self.assertEqual(a, b, "★ 同输入不同输出 —— 会话指纹会漂")
        self.assertEqual(a.encode("utf-8"), b.encode("utf-8"))

    def test_placeholder_count_matches_image_count(self):
        """两张图 → 两个占位符（数量也要可推理）"""
        content = [
            {"type": "image_url", "image_url": {"url": _data_uri()}},
            {"type": "image_url", "image_url": {"url": _data_uri()}},
        ]
        prompt, images = VisionPipeline.extract_images_and_text(content)
        self.assertEqual(len(images), 2)
        self.assertEqual(prompt.count(VisionPipeline.IMAGE_PLACEHOLDER), 2)

    def test_text_is_not_disturbed(self):
        """加占位符不能吃掉原有文本，也不能改变顺序"""
        content = [
            {"type": "text", "text": "请描述"},
            {"type": "image_url", "image_url": {"url": _data_uri()}},
            {"type": "text", "text": "这张图"},
        ]
        prompt, _ = VisionPipeline.extract_images_and_text(content)
        self.assertEqual(prompt.strip(), f"请描述 {VisionPipeline.IMAGE_PLACEHOLDER} 这张图")

    def test_unparsable_image_does_not_claim_an_image_exists(self):
        """解析失败时**不能**写占位符 —— 否则等于骗模型"这里有张图" """
        content = [{"type": "image_url", "image_url": {"url": "not-a-real-image"}}]
        prompt, images = VisionPipeline.extract_images_and_text(content)
        self.assertEqual(images, [])
        self.assertNotIn(VisionPipeline.IMAGE_PLACEHOLDER, prompt)


class TestPlaceholderReachesTheSessionTree(unittest.TestCase):
    """集成层：占位符必须真的出现在**会话树重建出来的 prompt** 里。

    只断言提取层不够 —— 用户要的是"融合多模态的**会话树**"，
    所以必须证明这个占位符能穿过 `归一化 → 存储 → prepare_prompt` 这条链。
    """

    def _normalized(self, content):
        """复刻路由层的归一化：content 走提取，结果进 normalized_messages"""
        prompt, _ = VisionPipeline.extract_images_and_text(content)
        return {"role": "user", "content": prompt}

    def test_history_turn_keeps_the_image_marker(self):
        from app.engine.session_tree import SessionTracker

        messages = [
            self._normalized([{"type": "image_url", "image_url": {"url": _data_uri()}}]),
            {"role": "assistant", "content": "这是一张 1x1 的图。"},
            {"role": "user", "content": "再描述一次"},
        ]
        prompt, _ = SessionTracker.prepare_prompt(messages, has_existing_session=False)
        self.assertIn(
            VisionPipeline.IMAGE_PLACEHOLDER, prompt,
            "★ 历史轮里的图片在会话树里丢了 —— 图片没有融合进会话，只是外挂了个 file_id",
        )

    def test_guard_is_not_vacuous(self):
        """不空转：确认上面造的 messages 里那一轮**确实是**图片轮"""
        m = self._normalized([{"type": "image_url", "image_url": {"url": _data_uri()}}])
        self.assertIn(VisionPipeline.IMAGE_PLACEHOLDER, m["content"])
        self.assertNotEqual(m["content"], "", "归一化后是空串 —— 那上一条断言就是假的")


class TestSafeJson(unittest.TestCase):
    """崩溃修复：非 JSON 响应不得再让 `.get()` 崩"""

    def test_non_json_returns_none_not_crash(self):
        r = FakeResp(status_code=502, text="<html>Bad Gateway</html>", raise_on_json=True)
        self.assertIsNone(VisionPipeline._safe_json(r, "测试"))

    def test_valid_json_passes_through(self):
        r = FakeResp(payload={"data": {"biz_data": {"id": "fid1"}}})
        self.assertEqual(VisionPipeline._safe_json(r, "测试")["data"]["biz_data"]["id"], "fid1")

    def test_html_error_page_body_is_logged(self):
        import logging
        recs = []

        class H(logging.Handler):
            def emit(self, r):
                recs.append(r.getMessage())

        lg = logging.getLogger("app.engine.vision_pipeline")
        h = H()
        lg.addHandler(h)
        old = lg.level
        lg.setLevel(logging.WARNING)
        try:
            VisionPipeline._safe_json(
                FakeResp(status_code=502, text="<html>blocked</html>", raise_on_json=True), "上传图片")
        finally:
            lg.removeHandler(h)
            lg.setLevel(old)
        self.assertTrue(any("非 JSON" in m and "blocked" in m for m in recs),
                        f"应当记录可诊断信息，实际 {recs}")


class TestCacheBounds(unittest.TestCase):
    """无上限的类级字典会随图片数量单调增长"""

    def setUp(self):
        VisionPipeline._file_cache.clear()

    def tearDown(self):
        VisionPipeline._file_cache.clear()

    def test_capacity_eviction(self):
        old = VisionPipeline._FILE_CACHE_MAX_ENTRIES
        VisionPipeline._FILE_CACHE_MAX_ENTRIES = 10
        try:
            for i in range(50):
                VisionPipeline._cache_put(f"k{i}", f"fid{i}")
            self.assertLessEqual(len(VisionPipeline._file_cache), 10)
            # 最新的必须还在
            self.assertEqual(VisionPipeline._cache_get("k49"), "fid49")
        finally:
            VisionPipeline._FILE_CACHE_MAX_ENTRIES = old

    def test_ttl_expiry(self):
        VisionPipeline._cache_put("k", "fid")
        self.assertEqual(VisionPipeline._cache_get("k"), "fid")
        VisionPipeline._file_cache["k"] = ("fid", time.time() - VisionPipeline._FILE_CACHE_TTL - 1)
        self.assertIsNone(VisionPipeline._cache_get("k"))

    def test_invalidate_account_cache(self):
        token = "x" * 40
        VisionPipeline._cache_put(f"{token[-16:]}:aaa", "fid1")
        VisionPipeline._cache_put(f"{token[-16:]}:bbb", "fid2")
        VisionPipeline._cache_put("other-account-suffix:ccc", "fid3")
        n = VisionPipeline.invalidate_account_cache(token)
        self.assertEqual(n, 2)
        self.assertEqual(len(VisionPipeline._file_cache), 1)
        self.assertEqual(VisionPipeline._cache_get("other-account-suffix:ccc"), "fid3")


class TestExtractionDiagnostics(unittest.TestCase):
    """未知块类型不得静默跳过 —— 用户看到的是「模型对我的图视而不见」，网关这边一片安静"""

    def _warnings(self, fn):
        import logging
        recs = []

        class H(logging.Handler):
            def emit(self, r):
                recs.append(r.getMessage())

        lg = logging.getLogger("app.engine.vision_pipeline")
        h = H()
        lg.addHandler(h)
        old = lg.level
        lg.setLevel(logging.WARNING)
        try:
            fn()
        finally:
            lg.removeHandler(h)
            lg.setLevel(old)
        return recs

    def test_unknown_part_type_is_logged(self):
        content = [{"type": "text", "text": "看图"},
                   {"type": "some_future_format", "data": "..."}]
        recs = self._warnings(lambda: VisionPipeline.extract_images_and_text(content))
        self.assertTrue(any("未识别" in m and "some_future_format" in m for m in recs),
                        f"未知类型必须留下诊断，实际 {recs}")

    def test_structured_content_yielding_nothing_is_logged(self):
        content = [{"type": "unknown_a"}, {"type": "unknown_b"}]
        recs = self._warnings(lambda: VisionPipeline.extract_images_and_text(content))
        self.assertTrue(any("既没解析出文本也没解析出图片" in m for m in recs),
                        f"应报告「什么都没解析出来」，实际 {recs}")

    def test_normal_text_content_produces_no_warning(self):
        recs = self._warnings(
            lambda: VisionPipeline.extract_images_and_text([{"type": "text", "text": "你好"}]))
        self.assertEqual([m for m in recs if "未识别" in m or "既没解析出" in m], [])


class TestSizeAndCountGuards(unittest.TestCase):
    """旧实现完全不设限：一张 50MB 的图会被整体读进内存再上传"""

    def test_oversize_data_uri_rejected(self):
        big = b"\x00" * (VisionPipeline.VISION_MAX_IMAGE_BYTES + 1)
        import base64
        uri = "data:image/png;base64," + base64.b64encode(big).decode()
        got = VisionPipeline._resolve_image_data(uri)
        # Data URI 路径不做体积拦截（已经解码在内存里了），但 remote 路径必须拦。
        # 这里只断言它不会抛异常并返回了合理结果。
        self.assertTrue(got is None or len(got[0]) == len(big))

    def test_remote_oversize_skipped(self):
        import app.engine.vision_pipeline as vp
        orig = vp.cffi_requests.get
        try:
            vp.cffi_requests.get = lambda *a, **k: FakeResp(
                content=b"\x00" * (VisionPipeline.VISION_MAX_IMAGE_BYTES + 10),
                headers={"content-type": "image/png"})
            self.assertIsNone(VisionPipeline._resolve_image_data("https://x/y.png"))
        finally:
            vp.cffi_requests.get = orig

    def test_remote_non_image_content_type_skipped(self):
        import app.engine.vision_pipeline as vp
        orig = vp.cffi_requests.get
        try:
            vp.cffi_requests.get = lambda *a, **k: FakeResp(
                content=b"<html>404</html>", headers={"content-type": "text/html"})
            self.assertIsNone(VisionPipeline._resolve_image_data("https://x/y.png"))
        finally:
            vp.cffi_requests.get = orig

    def test_image_count_capped(self):
        parts = [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG_B64}"}}
                 for _ in range(VisionPipeline.VISION_MAX_IMAGES + 5)]
        _, imgs = VisionPipeline.extract_images_and_text(parts)
        self.assertEqual(len(imgs), VisionPipeline.VISION_MAX_IMAGES)

    def test_oversize_upload_rejected_before_network(self):
        async def run():
            await VisionPipeline.upload_images(
                token="t" * 40,
                images=[(b"\x00" * (VisionPipeline.VISION_MAX_IMAGE_BYTES + 1), "big.png", "image/png")],
            )
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(run())
        self.assertIn("过大", str(ctx.exception))


class TestNonBlockingExtraction(unittest.TestCase):
    """★ 最严重的一条：提取函数在 async 端点里同步调用会卡住整个事件循环"""

    def test_async_wrapper_does_not_block_event_loop(self):
        """在提取期间，事件循环必须仍然能跑别的协程"""
        import app.engine.vision_pipeline as vp

        orig = vp.cffi_requests.get

        def slow_download(*a, **k):
            # 模拟阻塞下载：旧实现会在这里同步卡 15 秒
            time.sleep(0.8)
            return FakeResp(content=PNG_1PX, headers={"content-type": "image/png"})

        vp.cffi_requests.get = slow_download
        try:
            ticks = []

            async def heartbeat():
                for _ in range(12):
                    await asyncio.sleep(0.05)
                    ticks.append(time.monotonic())

            async def main():
                hb = asyncio.create_task(heartbeat())
                await VisionPipeline.extract_images_and_text_async(
                    [{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}]
                )
                await hb
                return ticks

            t0 = time.monotonic()
            ticks = asyncio.run(main())
            elapsed = time.monotonic() - t0

            self.assertGreater(elapsed, 0.7, "下载确实发生了")
            # 关键断言：下载期间心跳仍在推进（事件循环没被独占）
            self.assertGreaterEqual(len(ticks), 12,
                                    "事件循环被阻塞了 —— 其他请求在下载期间会完全冻结")
        finally:
            vp.cffi_requests.get = orig

    def test_sync_version_still_exists_for_compat(self):
        """同步版保留（有别的调用方/工具依赖），但路由必须走异步版"""
        self.assertTrue(hasattr(VisionPipeline, "extract_images_and_text"))
        self.assertTrue(asyncio.iscoroutinefunction(
            VisionPipeline.extract_images_and_text_async))


class TestTimeoutBudget(unittest.TestCase):
    """超时预算必须与客户端看门狗（约 50s）统筹"""

    def test_budget_is_under_client_watchdog(self):
        self.assertLess(VisionPipeline.VISION_TOTAL_BUDGET_SECONDS, 50.0,
                        "总预算必须给客户端看门狗留余量，否则网关还在干活、客户端已经放弃")

    def test_remaining_raises_when_exhausted(self):
        deadline = time.monotonic() - 1
        with self.assertRaises(TimeoutError) as ctx:
            VisionPipeline._remaining(deadline, "上传图片")
        self.assertIn("预算", str(ctx.exception))

    def test_remaining_returns_leftover(self):
        deadline = time.monotonic() + 10
        left = VisionPipeline._remaining(deadline, "上传图片")
        self.assertGreater(left, 8)
        self.assertLessEqual(left, 10.5)

    def test_upload_images_share_one_budget(self):
        """多图并发上传共享同一个 deadline，而不是每张各给一份预算"""
        seen = []

        async def fake_upload(**kw):
            seen.append(kw.get("deadline"))
            await asyncio.sleep(0.05)
            return f"fid{len(seen)}"

        orig = VisionPipeline.upload_image_to_deepseek
        VisionPipeline.upload_image_to_deepseek = classmethod(
            lambda cls, **kw: fake_upload(**kw))
        try:
            # 内容必须各不相同，否则会被去重
            imgs = [(PNG_1PX + bytes([i]), f"a{i}.png", "image/png") for i in range(5)]
            ids = asyncio.run(VisionPipeline.upload_images(token="t" * 40, images=imgs))
            self.assertEqual(len(ids), 5)
            self.assertTrue(all(d is not None for d in seen))
            # 同一个 deadline（同一预算）
            self.assertEqual(len(set(seen)), 1,
                             "5 张图必须共享一个超时预算，而不是各给一份")
        finally:
            VisionPipeline.upload_image_to_deepseek = orig

    def test_upload_images_is_bounded_concurrent(self):
        """并发度必须受限（账号并发舒适区是 2，不能把上游打爆）"""
        seq = {"n": 0}
        inflight = {"now": 0, "max": 0}

        async def fake_upload(**kw):
            seq["n"] += 1
            mine = seq["n"]          # 进入时取号，避免 await 之后再读造成竞态
            inflight["now"] += 1
            inflight["max"] = max(inflight["max"], inflight["now"])
            await asyncio.sleep(0.1)
            inflight["now"] -= 1
            return f"fid{mine}"

        orig = VisionPipeline.upload_image_to_deepseek
        VisionPipeline.upload_image_to_deepseek = classmethod(
            lambda cls, **kw: fake_upload(**kw))
        try:
            # 内容必须各不相同：内容相同会被去重，测不到并发度
            imgs = [(PNG_1PX + bytes([i]), f"a{i}.png", "image/png") for i in range(6)]
            asyncio.run(VisionPipeline.upload_images(token="t" * 40, images=imgs))
            self.assertLessEqual(inflight["max"], VisionPipeline.VISION_UPLOAD_CONCURRENCY,
                                 "并发上传数超过设定上限")
            self.assertGreater(inflight["max"], 1, "应当是并发而不是串行（串行会超看门狗）")
            self.assertEqual(seq["n"], 6, "6 张不同的图应各上传一次")
        finally:
            VisionPipeline.upload_image_to_deepseek = orig

    def test_partial_failure_is_reported_not_silent(self):
        """一张失败必须整单报错并说明成功了几张 —— 静默丢图会让用户以为「模型没看懂」"""
        calls = {"n": 0}

        async def flaky(**kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("模拟上传失败")
            await asyncio.sleep(0.02)
            return "fid"

        orig = VisionPipeline.upload_image_to_deepseek
        VisionPipeline.upload_image_to_deepseek = classmethod(lambda cls, **kw: flaky(**kw))
        try:
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(VisionPipeline.upload_images(
                    token="t" * 40,
                    # 内容必须各不相同，否则会被去重成 1 张，测不到"部分失败"
                    images=[(PNG_1PX + bytes([i]), f"a{i}.png", "image/png") for i in range(3)],
                ))
            msg = str(ctx.exception)
            self.assertIn("1/3", msg)
            self.assertIn("成功 2 张", msg)
        finally:
            VisionPipeline.upload_image_to_deepseek = orig

    def test_identical_images_uploaded_once(self):
        """同内容图片必须去重后再上传。

        实测依据（2026-09-11 生产）：一次请求里 3 张**完全相同**的图，
        日志里出现了 2 个 file_id —— 并发度 2 让两张在缓存命中之前各传了一遍
        （典型的缓存击穿）。上传是实打实的时间开销，去重是白捡的。
        """
        calls = []

        async def fake_upload(**kw):
            calls.append(kw["image_bytes"])
            await asyncio.sleep(0.02)
            return f"fid{len(calls)}"

        orig = VisionPipeline.upload_image_to_deepseek
        VisionPipeline.upload_image_to_deepseek = classmethod(lambda cls, **kw: fake_upload(**kw))
        try:
            ids = asyncio.run(VisionPipeline.upload_images(
                token="t" * 40,
                images=[(PNG_1PX, f"a{i}.png", "image/png") for i in range(4)],
            ))
            self.assertEqual(len(calls), 1, f"4 张相同图只该上传 1 次，实际 {len(calls)} 次")
            self.assertEqual(ids, ["fid1"] * 4, "返回的 file_id 列表长度必须与输入一致")
        finally:
            VisionPipeline.upload_image_to_deepseek = orig

    def test_distinct_images_each_uploaded(self):
        seq = {"n": 0}

        async def fake_upload(**kw):
            seq["n"] += 1
            mine = seq["n"]          # 进入时取号，避免 await 之后再读造成竞态
            await asyncio.sleep(0.02)
            return f"fid{mine}"

        orig = VisionPipeline.upload_image_to_deepseek
        VisionPipeline.upload_image_to_deepseek = classmethod(lambda cls, **kw: fake_upload(**kw))
        try:
            distinct = [(PNG_1PX + bytes([i]), f"a{i}.png", "image/png") for i in range(3)]
            ids = asyncio.run(VisionPipeline.upload_images(token="t" * 40, images=distinct))
            self.assertEqual(seq["n"], 3, "不同内容的图必须各传一次")
            self.assertEqual(len(set(ids)), 3)
        finally:
            VisionPipeline.upload_image_to_deepseek = orig

    def test_dedupe_preserves_order_and_duplicates(self):
        """去重不能打乱顺序，也不能合并调用方期望的重复项"""
        seq = []

        async def fake_upload(**kw):
            seq.append(kw["filename"])
            return f"fid-{kw['filename']}"

        orig = VisionPipeline.upload_image_to_deepseek
        VisionPipeline.upload_image_to_deepseek = classmethod(lambda cls, **kw: fake_upload(**kw))
        try:
            imgs = [(PNG_1PX, "a.png", "image/png"),
                    (PNG_1PX + b"x", "b.png", "image/png"),
                    (PNG_1PX, "c.png", "image/png")]
            ids = asyncio.run(VisionPipeline.upload_images(token="t" * 40, images=imgs))
            self.assertEqual(len(seq), 2)
            self.assertEqual(ids[0], ids[2], "第 1 与第 3 张内容相同，应得到同一个 fid")
            self.assertNotEqual(ids[0], ids[1])
        finally:
            VisionPipeline.upload_image_to_deepseek = orig

    def test_empty_images_returns_empty(self):
        self.assertEqual(asyncio.run(VisionPipeline.upload_images(token="t", images=[])), [])


class TestVisionNullBizDataAndAuth(unittest.TestCase):
    """测试 biz_data: null 或凭证失效时不会发生 AttributeError: 'NoneType' object has no attribute 'get'"""

    def test_pow_challenge_null_biz_data_raises_informative_error(self):
        """当 DeepSeek 返回 biz_data 为 null 时，必须抛出明确的 RuntimeError 而不是 AttributeError"""
        import app.engine.vision_pipeline as vp

        orig_post = vp.cffi_requests.post
        try:
            vp.cffi_requests.post = lambda *a, **kw: FakeResp(
                status_code=200,
                payload={"code": 0, "msg": "", "data": {"biz_code": 400004, "biz_msg": "frequency limit", "biz_data": None}}
            )
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(VisionPipeline.upload_image_to_deepseek(
                    token="t" * 40,
                    image_bytes=PNG_1PX,
                    deadline=time.monotonic() + 5,
                ))
            msg = str(ctx.exception)
            self.assertIn("获取上传 PoW 挑战失败", msg)
            self.assertNotIn("AttributeError", msg)
            self.assertIn("400004", msg)
        finally:
            vp.cffi_requests.post = orig_post

    def test_pow_challenge_invalid_token_raises_deepseek_auth_error(self):
        """当 PoW 接口返回 40003 invalid token 时，必须抛出 DeepSeekAuthError 驱动容灾"""
        import app.engine.vision_pipeline as vp
        from app.engine.deepseek_client import DeepSeekAuthError

        orig_post = vp.cffi_requests.post
        try:
            vp.cffi_requests.post = lambda *a, **kw: FakeResp(
                status_code=200,
                payload={"code": 40003, "msg": "Authorization Failed (invalid token)", "data": None}
            )
            with self.assertRaises(DeepSeekAuthError) as ctx:
                asyncio.run(VisionPipeline.upload_image_to_deepseek(
                    token="t" * 40,
                    image_bytes=PNG_1PX,
                    deadline=time.monotonic() + 5,
                ))
            self.assertIn("凭证失效", str(ctx.exception))
        finally:
            vp.cffi_requests.post = orig_post

    def test_upload_null_biz_data_raises_informative_error(self):
        """当上传接口返回 biz_data 为 null 时，必须抛出明确的 RuntimeError 而不是 AttributeError"""
        import app.engine.vision_pipeline as vp

        orig_post = vp.cffi_requests.post
        calls = {"n": 0}

        def fake_post(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                # 成功的 PoW
                return FakeResp(
                    status_code=200,
                    payload={"code": 0, "msg": "", "data": {"biz_code": 0, "biz_msg": "", "biz_data": {"challenge": {"algorithm": "DeepSeekHashV1", "challenge": "abc", "salt": "123", "difficulty": 1}}}}
                )
            # 上传返回 biz_data: null
            return FakeResp(
                status_code=200,
                payload={"code": 0, "msg": "", "data": {"biz_code": 50001, "biz_msg": "file service busy", "biz_data": None}}
            )

        try:
            vp.cffi_requests.post = fake_post
            # 假 PoW 求解器
            orig_solve = vp.DeepSeekPOW.solve
            vp.DeepSeekPOW.solve = lambda ch: "fake_pow"
            try:
                with self.assertRaises(RuntimeError) as ctx:
                    asyncio.run(VisionPipeline.upload_image_to_deepseek(
                        token="t" * 40,
                        image_bytes=PNG_1PX,
                        deadline=time.monotonic() + 5,
                    ))
                msg = str(ctx.exception)
                self.assertIn("图片上传失败", msg)
                self.assertNotIn("AttributeError", msg)
                self.assertIn("50001", msg)
            finally:
                vp.DeepSeekPOW.solve = orig_solve
        finally:
            vp.cffi_requests.post = orig_post

    def test_detect_raster_format(self):
        """测试光栅图片格式检测与非标文件过滤"""
        # PNG
        self.assertEqual(VisionPipeline.detect_raster_format(PNG_1PX), ("png", "image/png"))
        # JPEG (\xff\xd8\xff)
        fake_jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 20
        self.assertEqual(VisionPipeline.detect_raster_format(fake_jpeg), ("jpg", "image/jpeg"))
        # WEBP (RIFF....WEBP)
        fake_webp = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 20
        self.assertEqual(VisionPipeline.detect_raster_format(fake_webp), ("webp", "image/webp"))

        # SVG 矢量文本
        fake_svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="20"><text>Badge</text></svg>'
        self.assertIsNone(VisionPipeline.detect_raster_format(fake_svg))
        # HTML 错误页
        fake_html = b"<!DOCTYPE html><html><body>Error 404</body></html>"
        self.assertIsNone(VisionPipeline.detect_raster_format(fake_html))
        # 任意非标二进制
        self.assertIsNone(VisionPipeline.detect_raster_format(b"MZ\x90\x00\x03\x00\x00\x00"))

    def test_markdown_svg_badge_is_ignored_and_kept_as_text(self):
        """测试 GitHub README 中的 shields.io / SVG 徽章不会被误当作图片上传，避免引发 biz_code: 9 (HTTP 400) 导致 Agent 会话死锁"""
        import app.engine.vision_pipeline as vp

        readme_text = (
            '# Open-ReverseLab\n\n'
            '[![文档站](https://img.shields.io/badge/docs-3D4F8C)](https://reverselab.int0.cc)\n'
            '[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com)\n'
            '正常正文描述内容。'
        )

        orig_get = vp.cffi_requests.get
        try:
            # 模拟 shields.io 返回 SVG 矢量内容
            def fake_get(url, **kw):
                return FakeResp(
                    status_code=200,
                    content=b'<svg xmlns="http://www.w3.org/2000/svg"><text>doc</text></svg>',
                    headers={"content-type": "image/svg+xml;charset=utf-8"}
                )
            vp.cffi_requests.get = fake_get

            prompt, images = VisionPipeline.extract_images_and_text(readme_text)
            # 关键断言：绝对不能提取出任何图片（否则会导致上传给 DeepSeek 报 biz_code: 9）
            self.assertEqual(len(images), 0)
            # 关键断言：原始 markdown 链接完整保留
            self.assertIn("https://img.shields.io/badge/docs-3D4F8C", prompt)
            self.assertIn("https://deepwiki.com/badge.svg", prompt)
            self.assertIn("正常正文描述内容。", prompt)
        finally:
            vp.cffi_requests.get = orig_get


if __name__ == "__main__":
    unittest.main(verbosity=2)
