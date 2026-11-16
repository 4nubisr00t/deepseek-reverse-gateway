import unittest
from unittest.mock import patch, MagicMock
import asyncio

from app.engine.vision_pipeline import (
    VisionPipeline,
    DeepSeekMutedError,
    UnsupportedFileTypeError,
)
from app.engine.account_pool import AccountPool, AccountRuntime
from app.core.security import disown_account


class TestVisionResilienceAndCircuitBreaker(unittest.TestCase):
    def test_vision_max_images_enforced_on_string_content(self):
        """测试在文本中内嵌超过最大限制的图片时，强制截断并丢弃超出部分"""
        # 生成 15 张内嵌 markdown 图片
        md_content = "".join([f"![img{i}](data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=)" for i in range(15)])
        prompt, images = VisionPipeline.extract_images_and_text(md_content)
        self.assertEqual(len(images), VisionPipeline.VISION_MAX_IMAGES)
        self.assertLessEqual(len(images), 8)

    def test_temporary_mute_does_not_disown_account(self):
        """测试临时熔断退避(is_muted=True, permanent=False)绝对不能将账号从 Key 归属中剥离"""
        pool = AccountPool()
        acc = AccountRuntime({
            "account_label": "test_keep_owner@example.com",
            "token": "dummy_token_12345",
            "password": "pwd",
        })
        pool.accounts = [acc]

        # 模拟 disown_account 被调用
        with patch("app.core.security.disown_account") as mock_disown:
            pool.release_account(acc, is_muted=True, permanent=False)
            mock_disown.assert_not_called()
            self.assertEqual(acc.status, "MUTED")
            self.assertFalse(acc.is_valid)

    def test_permanent_invalid_disowns_account(self):
        """测试永久失效(permanent=True)时才执行 disown_account 剥离归属"""
        pool = AccountPool()
        acc = AccountRuntime({
            "account_label": "test_disown_perm@example.com",
            "token": "dummy_token_67890",
            "password": "pwd",
        })
        pool.accounts = [acc]

        with patch("app.core.security.disown_account") as mock_disown:
            pool.release_account(acc, is_muted=False, permanent=True)
            mock_disown.assert_called_once_with("test_disown_perm@example.com")
            self.assertEqual(acc.status, "INVALID")


if __name__ == "__main__":
    unittest.main()
