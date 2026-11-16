"""
ds-gateway API 错误脱敏与管理员故障映射模块 (Error Catalog & Sanitizer)

设计目标：
1. 【对外严格脱敏】：杜绝向客户端暴露后端号池架构、账号并发数、多账号轮换特征、
   上游厂商 (DeepSeek) 标识、拦截命中词、内部违规码 (biz_code: 5) 等敏感信息，防止被逆向与指纹探测。
2. 【对内详尽记录】：所有技术细节（涉及账号标签、原始错误码、风控特征词、上游链路追踪）
   全量输出至服务器内部日志（logger / journalctl），供管理员精准排障。
3. 【管理员状态码与故障映射表】：为系统运维人员提供直观的状态码、内部真实成因与处理指南对照。
"""

import logging
from typing import Any, Dict, Optional
from fastapi import HTTPException

logger = logging.getLogger("ds_gateway.audit")


# ─────────────────────────────────────────────────────────────────────────────
# 一、对外脱敏的通用标准错误描述 (Client-Facing Sanitized Messages)
# ─────────────────────────────────────────────────────────────────────────────

#: 400 敏感词/安全合规拦截通用话术（保留 code 以供 SDK 识别，去除特征词、规则与封号细节）
CLIENT_MSG_CONTENT_POLICY = "The request was rejected due to content policy violations."

#: 429 账号池并发额度满载通用话术（隐藏号池规模、每号 2 并发与底层轮换机制）
CLIENT_MSG_CONCURRENCY_LIMIT = "Rate limit reached. Please reduce concurrency or retry later."

#: 429 日配额超限通用话术
CLIENT_MSG_DAILY_QUOTA = "Daily request quota exceeded. Please retry tomorrow."

#: 429 订阅周期配额超限通用话术
CLIENT_MSG_SUBSCRIPTION_QUOTA = "Subscription quota exceeded for the current billing cycle."

#: 429 上游限流通用话术（隐藏账号禁言/空流/上游封禁细节）
CLIENT_MSG_UPSTREAM_RATE_LIMIT = "Rate limit reached. Please retry in a few seconds."

#: 502 上游网关错误（隐藏上游真实报错、账号失效、biz_code 与换号重试逻辑）
CLIENT_MSG_UPSTREAM_BAD_GATEWAY = "Upstream service returned an error. Please retry."

#: 502 上游会话建立失败（隐藏多账号切换与上游底层初始化细节）
CLIENT_MSG_UPSTREAM_SESSION_FAILED = "Upstream session initialization failed. Please retry later."

#: 502 多模态图片上传/解析失败（隐藏官方文件服务细节与内部超时）
CLIENT_MSG_MEDIA_PROCESSING_FAILED = "Failed to process media attachment. Please retry."

#: 503 服务繁忙/服务不可用（隐藏全部账号熔断/未分配账号池细节）
CLIENT_MSG_SERVICE_UNAVAILABLE = "Service temporarily unavailable due to high load. Please retry later."

#: 503 上游官方服务器过载
CLIENT_MSG_UPSTREAM_BUSY = "Upstream server is temporarily busy. Please retry later."


# ─────────────────────────────────────────────────────────────────────────────
# 二、快捷异常构造器 (带有详细内部审计日志)
# ─────────────────────────────────────────────────────────────────────────────

def raise_sanitized_sensitive_block(
    matched_category: str,
    matched_keyword: str,
    rule_desc: str,
    key_name: str,
    key_id: Optional[str] = None,
) -> None:
    """触发敏感词拦截：对内详细记录特征，对外仅返回合规通用报错。"""
    logger.warning(
        f"[Audit-SensitiveFilter] Key '{key_name}' (id={key_id}) 命中风控规则: "
        f"分类=[{matched_category}], 特征='{matched_keyword}', 规则='{rule_desc}'"
    )
    raise HTTPException(
        status_code=400,
        detail={
            "error": {
                "message": CLIENT_MSG_CONTENT_POLICY,
                "type": "invalid_request_error",
                "code": "sensitive_keyword_detected",
            }
        },
    )


def raise_sanitized_concurrency_limit(
    key_label: str,
    retry_after: int,
    internal_detail: Optional[str] = None,
) -> None:
    """触发并发限制：对内记录号池水位，对外输出标准 429。"""
    logger.warning(
        f"[Audit-Concurrency] Key '{key_label}' 触发并发限制 (Retry-After: {retry_after}s): {internal_detail}"
    )
    raise HTTPException(
        status_code=429,
        detail=CLIENT_MSG_CONCURRENCY_LIMIT,
        headers={"Retry-After": str(retry_after)},
    )


def raise_sanitized_pool_empty(
    key_label: str,
    reason: str,
) -> None:
    """号池无可用号或未分配：对内记录号池诊断，对外输出通用 503。"""
    logger.error(
        f"[Audit-AccountPool] Key '{key_label}' 无法获取可用账号 (原因: {reason})"
    )
    raise HTTPException(
        status_code=503,
        detail=CLIENT_MSG_SERVICE_UNAVAILABLE,
    )


def raise_sanitized_upstream_error(
    status_code: int,
    client_msg: str,
    internal_reason: str,
    account_label: Optional[str] = None,
    retry_after: Optional[int] = None,
) -> None:
    """上游抛错：对内记录账号与真实报错，对外统一输出脱敏报错。"""
    headers = {}
    if retry_after:
        headers["Retry-After"] = str(retry_after)
    logger.error(
        f"[Audit-UpstreamError] HTTP {status_code} (账号: {account_label or 'none'}): {internal_reason}"
    )
    raise HTTPException(
        status_code=status_code,
        detail=client_msg,
        headers=headers if headers else None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 三、状态码与管理员原因映射表定义 (供文档与后台接口使用)
# ─────────────────────────────────────────────────────────────────────────────

STATUS_CODE_CATALOG: Dict[str, Dict[str, Any]] = {
    "400_CONTENT_POLICY": {
        "status_code": 400,
        "error_type": "invalid_request_error",
        "error_code": "sensitive_keyword_detected",
        "client_message": CLIENT_MSG_CONTENT_POLICY,
        "internal_cause": "用户请求文本或提问内容命中了敏感词防护词库（涉政、暴恐、攻防越狱等特征）。",
        "troubleshooting": "在服务器日志搜索 `[Audit-SensitiveFilter]` 查看命中词。若为合法学术研究/内部压测，可在管理后台「圣钥权限」中关闭该 Key 的敏感词过滤开关（sensitive_filter: false）。",
    },
    "400_BAD_REQUEST": {
        "status_code": 400,
        "error_type": "invalid_request_error",
        "error_code": "bad_request",
        "client_message": "Invalid request payload or parameters.",
        "internal_cause": "请求 JSON 格式损坏、messages 数组为空、或传入了不受支持的多模态格式。",
        "troubleshooting": "检查客户端发出的 HTTP Body 是否符合 OpenAI Chat Completions 规范。",
    },
    "401_UNAUTHORIZED": {
        "status_code": 401,
        "error_type": "auth_error",
        "error_code": "invalid_api_key",
        "client_message": "Invalid API key or authorization header.",
        "internal_cause": "客户端未带 Authorization 头，或传入的 API Key 不存在、已被撤销（revoked）或禁用（enabled=false）。",
        "troubleshooting": "在后台「圣钥权限」中核对该 Key 是否处于激活状态。",
    },
    "403_FORBIDDEN": {
        "status_code": 403,
        "error_type": "auth_error",
        "error_code": "account_suspended",
        "client_message": "Access denied. Account suspended or expired.",
        "internal_cause": "用户账号已被封禁，或订阅套餐已到期被系统冻结。",
        "troubleshooting": "在后台用户列表检查该用户状态是否正常，是否有欠费或到期订单。",
    },
    "429_CONCURRENCY": {
        "status_code": 429,
        "error_type": "requests",
        "error_code": "concurrency_limit_exceeded",
        "client_message": CLIENT_MSG_CONCURRENCY_LIMIT,
        "internal_cause": "该 Key 名下绑定的账号（或公共池账号）并发达到上限（默认每账号 2 并发）。",
        "troubleshooting": "在后台查看账号池占用状态。若该客户有大并发长任务需求，可为其 Key 多分配几个专属做功号（子代理上限 = 专属号数 × 2）。",
    },
    "429_QUOTA_EXCEEDED": {
        "status_code": 429,
        "error_type": "requests",
        "error_code": "quota_exceeded",
        "client_message": CLIENT_MSG_DAILY_QUOTA,
        "internal_cause": "该 Key 的日请求次数或日 Token 配额已达到设定阈值（明日 0 点自动重置）。",
        "troubleshooting": "在后台「用量监控」中查看今日请求峰值，必要时在后台调高该 Key 的 daily_quota_requests / daily_quota_tokens。",
    },
    "429_UPSTREAM_RATE_LIMIT": {
        "status_code": 429,
        "error_type": "requests",
        "error_code": "upstream_rate_limited",
        "client_message": CLIENT_MSG_UPSTREAM_RATE_LIMIT,
        "internal_cause": "上游官方针对账号返回 rate_limit（发送过于频繁）或 0 字节空流，网关已尝试自动 Failover 换号重试但仍触顶。",
        "troubleshooting": "属于正常的上游防护冷却（通常冷却 20s 自动解冻）。若高发，建议通过脚本批量补充新号，扩充活跃可用号池储备。",
    },
    "502_SESSION_FAIL": {
        "status_code": 502,
        "error_type": "server_error",
        "error_code": "upstream_session_failed",
        "client_message": CLIENT_MSG_UPSTREAM_SESSION_FAILED,
        "internal_cause": "向官方创建会话失败（Token 失效、密码登录重登失败、或触发上游 WAF 403）。",
        "troubleshooting": "在后台「账号池」检查是否有账号标记为失效。使用批量检测/重新登录功能剔除失效账号。",
    },
    "502_UPSTREAM_ERROR": {
        "status_code": 502,
        "error_type": "server_error",
        "error_code": "bad_gateway",
        "client_message": CLIENT_MSG_UPSTREAM_BAD_GATEWAY,
        "internal_cause": "上游官方服务抛出非预期错误（如上游禁言 biz_code: 5、上游 500/502 等）。",
        "troubleshooting": "查看服务器日志 `[Audit-UpstreamError]` 提取原始上游 error_code。若命中 biz:5，该账号已被官方禁言，调度器已将其移入冷却池。",
    },
    "503_NO_ACCOUNTS": {
        "status_code": 503,
        "error_type": "server_error",
        "error_code": "no_available_accounts",
        "client_message": CLIENT_MSG_SERVICE_UNAVAILABLE,
        "internal_cause": "该 API Key 名下未分配账号（no_account_assigned），或系统所有账号均处于忙碌/冷却熔断状态。",
        "troubleshooting": "关键运维事项！请检查后台「圣钥权限」是否给该用户 Key 分配了专属号，或检查「账号池」中是否有足量的 ACTIVE 活跃账号。",
    },
    "503_UPSTREAM_BUSY": {
        "status_code": 503,
        "error_type": "server_error",
        "error_code": "upstream_busy",
        "client_message": CLIENT_MSG_UPSTREAM_BUSY,
        "internal_cause": "官方服务在 SSE 首帧抛出 upstream_busy 或官方服务器过载标识。",
        "troubleshooting": "官方机房算力打满时的临时限流，无需干预，客户端会在 5s 后根据 Retry-After 自动重试。",
    },
}
