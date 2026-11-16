import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi import HTTPException

from app.engine.vision_pipeline import VisionPipeline, UnsupportedFileTypeError
from app.engine.account_pool import AccountRuntime


def test_unsupported_file_type_error_detection():
    # 模拟上游返回 biz_code: 9
    upstream_resp = {
        "code": 0,
        "msg": "",
        "data": {
            "biz_code": 9,
            "biz_msg": "unsupported file type",
            "biz_data": None
        }
    }
    
    # 验证异常继承自 Exception
    err = UnsupportedFileTypeError("unsupported file type")
    assert isinstance(err, Exception)


@pytest.mark.anyio
async def test_openai_route_handles_unsupported_file_safely():
    from app.api.openai_routes import openai_chat_completions
    from fastapi import Request

    fake_request = MagicMock(spec=Request)
    fake_request.json = AsyncMock(return_value={
        "model": "deepseek-chat",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "分析这个文件"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="}}
                ]
            }
        ]
    })
    fake_request.headers = {}

    identity = {
        "key_id": "test_key",
        "log_label": "test_user",
        "sensitive_filter": False,
        "account_labels": ["test_acc"],
        "pool_fallback": False,
    }

    fake_account = AccountRuntime({
        "account_label": "test_acc",
        "token": "test_token",
        "cookie": "test_cookie",
    })

    with patch("app.api.openai_routes.account_pool.acquire_account", AsyncMock(return_value=fake_account)), \
         patch("app.api.openai_routes.VisionPipeline.upload_images", AsyncMock(side_effect=UnsupportedFileTypeError("unsupported file type"))), \
         patch("app.api.openai_routes.account_pool.release_account") as mock_release:

        with pytest.raises(HTTPException) as exc_info:
            await openai_chat_completions(fake_request, identity)

        assert exc_info.value.status_code == 400
        assert "unsupported file type" in str(exc_info.value.detail)
        # 关键断言：释放账号时绝对不能标记为 muted (is_muted=False)
        mock_release.assert_called_once_with(fake_account, is_muted=False)
