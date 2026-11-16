"""
ds-gateway 配置管理模块
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# ⚠️ 路径可被环境变量覆盖 —— 这不是为了方便，而是**测试安全所必需的**。
#
# 2026-09-11 真实事故：测试用例把 `config_manager.accounts` 换成假账号
# （`a@x` / `tok-a@x`）来测调度逻辑，而某条路径
# （`release_account` → `update_account` → `save()`）会把它**写进真实的 config.json**。
# tearDown 只还原了内存，文件已经被写坏：本地 config 的 3 个账号变成了假账号，
# `auto_replenish` 也被测试改成 False 并落盘。
#
# 这不是"本地小问题"：**如果那台机器是生产，25 个账号会被一次性清空**，
# 而 config.json 既没进 git、也没有备份。
# 所以 tests/conftest.py 会把 DSG_CONFIG_FILE 指到临时目录，
# 让测试永远碰不到真实配置文件。
CONFIG_FILE = Path(os.environ.get("DSG_CONFIG_FILE", str(PROJECT_ROOT / "config.json")))


class AccountModel(BaseModel):
    account_label: str
    token: str
    cookie: str = ""
    session_id: Optional[str] = None
    is_valid: bool = True
    mute_until: Optional[int] = None
    last_used: Optional[float] = None
    _password: Optional[str] = None
    _email: Optional[str] = None
    proxy: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════════
# T9 单模型收敛：对外一个模型，其余 ID 作为别名
# ══════════════════════════════════════════════════════════════════════════
#
# ## 为什么要收敛
#
# 实测（清单 T9）：上游 `model_type` 取 `default` / `expert` / `vision`
# → 上游**全部**返回 `conversation_mode: "DEFAULT"`，正文 **sha1 完全相同**；
# 只有**非法值**才 422。新网页端只剩一个统一入口，没有模式选择。
# 也就是说网关原先的"4 模型双梯队"是**对上游不存在的东西做路由**：
# 4 个 ID 在上游其实是同一条通道。
#
# ## 为什么别名必须保留
#
# 现有客户端配置里已经写死了这 4 个 ID（README、DSH、Cline、Cherry Studio 等）。
# 直接删掉其中任何一个都会让那部分客户端**立刻 400/404**——
# 那是"升级即炸"，不是收敛。所以：**对外继续接受全部旧 ID，但把它们全部
# 归一到同一个上游行为**。
#
# ## 等价性的判据（改这里的代码前先读这段）
#
# 4 个 ID 必须在**任何可观测行为**上不可区分，否则等于没收敛：
#   · 同一个上游通道（同一个 `model_type` 线值）
#   · 同一套 L0 提示词（同一个 conv_key 命名空间 → 同一套 prompt 前缀）
#   · 同一张模型卡（`/v1/models` 逐字段相同，`/v1/models/{id}` 也一样）
# 前两条靠 `normalize_model_id()` 在任何决策点**之前**归一完成。
#
# ⚠️ 注意：`reasoning_effort` 曾经按模型区分（pro=high / flash=low）。
# 收敛后 4 个 ID 报同一张卡，所以别名用户的 `reasoning_effort` 会变成 `high`。
# 这**不是**回归，而是本决策的必然结果——若各报各的，客户端仍会以为
# "换了模型"，收敛就没有发生。

#: 对外的**唯一**主模型 ID。新客户端配置只应使用这一个。
CANONICAL_MODEL_ID = "deepseek-v4.1-flash"

#: 主模型 ID 的展示名。
CANONICAL_MODEL_NAME = "DeepSeek-V4.1-Flash"

#: 主模型的模型卡描述。
CANONICAL_MODEL_DESCRIPTION = (
    "DeepSeek V4 统一推理模型 (深度思考 + 复杂推理 + 原生多模态 text+image)"
)

#: 旗舰强化模型 ID 与描述
PRO_MODEL_ID = "deepseek-v4-pro"
PRO_MODEL_NAME = "DeepSeek-V4-Pro"
PRO_MODEL_DESCRIPTION = (
    "DeepSeek V4.1 旗舰强化模型 (反说教突破 + 工业级硬核交付 + 极限推演 + 原生多模态)"
)

#: 别名 → 主模型 ID，**全部**归一到这里。
#: 左侧的键是历史 ID，任何一个都不允许再独立路由。
#:
#: ⚠️ 改主模型 ID 会**连带改掉所有 `conv_key`**（键里含 `model`）——
#: 也就是改的那一刻，所有在跑的会话绑定都变成不可命中：
#: 下一轮会新建上游会话、并且**可能换号**（`preferred_account` 取自绑定）。
#: 这是一次性代价，不是 bug；但要在**没有长任务在跑**的时候改。
#: 历史 ID 必须继续保留在别名表里，否则老客户端换个名字就直接被打断（T9 的教训）。
MODEL_ALIASES: Dict[str, str] = {
    CANONICAL_MODEL_ID: CANONICAL_MODEL_ID,
    # 旗舰强化模型与别名
    PRO_MODEL_ID: CANONICAL_MODEL_ID,
    "v4pro": CANONICAL_MODEL_ID,
    # 收敛前的 4 个历史 ID 与兼容别名
    "deepseek-v4-flash": CANONICAL_MODEL_ID,
    "deepseek-v4-pro-exp": CANONICAL_MODEL_ID,
    "deepseek-v4-flash-exp": CANONICAL_MODEL_ID,
    "deepseek-v4.1-flash-act": CANONICAL_MODEL_ID,
}

# ── 上线时同步更新：这两个常量进模型卡，方便客户端探测能力 ──

#: **我们保证能服务的**上下文上限（不是"理论上限"）。
#:
#: 这个数字**会直接决定客户端的压缩时机** —— DSH 的压缩阈值是
#: `context_window × 0.8`（`dsh-compaction-basic` 的 `DEFAULT_THRESHOLD_RATIO`）。
#:
#: 原来写的是 1048576（1M）。那让 DSH 的阈值落在 **838,861 tokens** ——
#: 而实测（清单 T24「实测断点 E37」）的真实上限是：
#:
#:     填充 3,200,000 字符（~800k tokens）→ ✅ 正常
#:     填充 6,400,000 字符（~1.6M tokens）→ ❌ 达到对话长度上限
#:
#: 也就是说 **DSH 要等到 839k 才开始压缩，而 800k 已经贴着线了** ——
#: 它不是不肯压，是我们把阈值告诉了它一个踩线的值。
#:
#: 现在声明 **800,000**（实测验证过能正常服务的量级）→ DSH 阈值降到
#: **640,000**，提前约 200k 压缩，从而**在撞墙之前**换好上下文。
#:
#: ⚠️ 调这个数要谨慎：调大 = 客户端压得更晚（更容易撞墙）；
#:    调小 = 压得更频繁（每次压缩本身就是一次额外的模型调用，有成本）。
CANONICAL_MODEL_CONTEXT_WINDOW = 800_000

#: 单次回复的最大输出长度。**注意与上下文窗口是两件事** ——
CANONICAL_MODEL_MAX_OUTPUT_TOKENS = 16384

# ── 上游 `model_type`：**恒定一个值**（2026-09-11 用真图实测改写） ──
#
# ## 之前错在哪
#
# T9 把这里表达成"推理档位"：有图 → `vision`，无图 → `expert`。
# 那是从一个**没验证过的推断**推出来的 —— T9 的实测只证明了
# "`model_type` 取不同值、正文 sha1 相同"，也就是**这个参数不改变行为**；
# 却从这一步被推成了"档位由有没有图决定"，还写进了代码、注释和 README。
#
# ## 实测（带真图的探针，一张 64×64 左红右蓝 PNG）
#
#   我们发出 model_type=expert → 上游回显 {"model_type":"expert"}  正文「左红右蓝」
#   我们发出 model_type=vision → 上游回显 {"model_type":"expert"}  正文「左红右蓝」
#   两次正文 sha1 完全相同
#
# ★ 关键在第二行：**上游把 `vision` 改写成了 `expert`**。
#   也就是说 `vision` 这个档位在上游**已经不存在了** —— 我们自己还在切，
#   对面早就归一了。这与公开信息一致：DeepSeek 已把**快速 / 专家 / 识图
#   三个模式合并成一个**（V4.1 Flash，原生多模态，不用再手动切换），
#   实测也证明**不切档位、直接带图引用就能被模型看见**。
#
# ## 所以现在
#
# 上游对取值**完全无视**（连回显都改写成 expert），但**仍校验合法性**：非法值 422。
# 正确做法是**恒定发一个合法值**，不再让"有没有图"影响它。
#
# ⛔ 不要删除请求体里的 `model_type`，也不要传白名单外的值 ——
#    删掉/传错 = 把每一个请求打成 422 故障。
UPSTREAM_MODEL_TYPE_DEFAULT = "default"
UPSTREAM_MODEL_TYPE_EXPERT = "expert"
UPSTREAM_MODEL_TYPE_VISION = "vision"

#: 我们**实际发往上游**的那个值。恒定，与"有没有图"无关。
UPSTREAM_MODEL_TYPE = UPSTREAM_MODEL_TYPE_EXPERT

#: 上游仍**接受**的 `model_type` 字面量集合（非法值 422）。
#: ⚠️ "接受"不等于"生效"：实测 `vision` 会被上游改写成 `expert`。
UPSTREAM_MODEL_TYPE_VALUES = frozenset({
    UPSTREAM_MODEL_TYPE_DEFAULT,
    UPSTREAM_MODEL_TYPE_EXPERT,
    UPSTREAM_MODEL_TYPE_VISION,
})

#: 主模型对外声明的默认推理档位（模型卡 `reasoning_effort` 字段，非上游参数）。
CANONICAL_REASONING_EFFORT = "medium"
PRO_REASONING_EFFORT = "max"

#: 统一暴露支持的推理档位列表（OpenAI/DSH/Anthropic/Responses 规范）
SUPPORTED_REASONING_EFFORTS = ["off", "low", "medium", "high", "max"]


def normalize_model_id(model: Optional[str]) -> str:
    """把客户端传来的任意模型 ID 归一到**唯一**主模型 ID。

    这是 T9 别名等价性的**唯一入口**：`openai_routes` / `claude_routes`
    必须在任何决策（会话键、提示词、推理档位、上游参数）之前调用它。

    未知 ID **不报错**，同样归一到主模型——这与收敛前的行为一致
    （旧代码用子串匹配兜底，任意未登记 ID 都能跑通，见 T9 之前的
    `is_pro = any(k in model_lower for k in ("pro", ...))` 兜底分支）。
    收紧成 400 会在客户端换个新名字时直接打断服务，收益为零。

    :param model: 客户端原始 `model` 字段（可能为 None / 空串）
    :return: 永远是 :data:`CANONICAL_MODEL_ID`
    """
    key = (model or "").strip().lower()
    # 已登记的别名走查表；未登记的（含空串、None）一律落到主模型。
    # 查表只是为了把"已知别名"与"未知新名字"在语义上区分开（便于将来加白名单），
    # 两者**当前**的返回值完全相同——这正是"别名不可区分"的要求。
    return MODEL_ALIASES.get(key, CANONICAL_MODEL_ID)


def is_known_model_id(model: Optional[str]) -> bool:
    """该 ID 是否是我们对外**登记过**的别名（用于日志/观测，不影响路由）。"""
    return (model or "").strip().lower() in MODEL_ALIASES


def is_pro_model(model: Optional[str]) -> bool:
    """判断模型 ID 是否为旗舰强化模型 (V4-Pro)。"""
    m = (model or "").strip().lower()
    return m in ("deepseek-v4-pro", "v4pro") or "v4pro" in m or "v4-pro" in m


def upstream_model_type() -> str:
    """本次请求发往上游的 `model_type`。

    这是一个**恒定值**，签名里**没有 `has_images` 参数**，这是刻意的：

    T9 曾经有过 `reasoning_tier_for(has_images)` —— 有图返回 `vision`、无图返回 `expert`，
    并声称"签名故意没有 `model` 参数"。但那个函数**保留了 `has_images`**，
    于是"档位"换了个维度继续存在，而且建立在"识图是独立模式"这个
    **当时没有验证过、现在已被证伪**的假设上。

    实测（真图探针）：把 `vision` 发上去，**上游回显改写成 `expert`**；
    不切档位直接带图引用，模型照样准确描述图片内容。
    → 档位与图片无关，所以这里不再接受任何参数。

    返回值**必定**属于 :data:`UPSTREAM_MODEL_TYPE_VALUES`，否则上游 422。
    """
    return UPSTREAM_MODEL_TYPE


class GatewayConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    admin_password: str = "admin123"
    require_api_key: bool = True
    cooldown_seconds: float = 3.0  # 单号调用后冷却时间（秒）
    max_session_turns: int = 15    # 单会话轮数上限（达到后自动分叉开辟新会话平滑轮换账号，避免 50+ 轮超长会话触发官方风控）
    rate_limit_cooldown_seconds: float = 20.0  # 遭遇 429 时的临时保护性冷却时间（秒）
    proxy: Optional[str] = None    # 出口代理，如 http://127.0.0.1:7890
    proxies: List[str] = Field(default_factory=list)  # 出口代理池，如 ["http://127.0.0.1:7891", "http://127.0.0.1:7892"]
    # ★ 登录（免码重登/上号）专用出口代理池 —— 与业务转发用 proxies 分开。
    # 官方按 **IP** 做登录频控(40029 IP_ACCESS_RESTRICTED)，业务转发和登录混用一个出口 IP
    # 会互相拖累；这里留空 = 沿用旧行为（直连/业务代理），填了则按账号哈希分散选择。
    relogin_proxies: List[str] = Field(default_factory=list)
    anti_cascade_same_ip: bool = True  # 遭遇官方禁言时，是否开启同IP连坐熔断保护
    mailcx_api_key: str = ""
    mailcx_domain: str = ""
    cf_mail_worker: str = ""
    cf_mail_domain: str = "example.com"
    passthrough: bool = False
    auto_replenish: bool = True     # 当有效账号低于阈值时是否自动后台洗礼补位
    min_active_accounts: int = 3   # 维持对外服务的最低有效账号数
    # ── 重构新增：防封号代理隔离、粘性降级与邮箱验证自注册配置 ──
    allow_direct_fallback_on_proxy_error: bool = False  # 代理异常时是否允许回退本机直连（默认关闭防裸奔连坐）
    sticky_micro_wait_seconds: float = 0.0              # 粘性会话满载微等待窗口（秒，生产建议 1.5~2.0）
    allow_sticky_drift_without_files: bool = False      # 无图粘性会话满载时是否允许跨号平滑重水化
    debug_raw_sse: bool = False                         # 是否逐行打印上游原始 SSE 帧 (默认关闭防日志洪泛)
    debug_log_messages: bool = False                    # 是否打印全量对话历史明细
    client_platform: str = "ios"                        # 上游通信协议端 ("ios" | "web"，可用环境变量 DSG_CLIENT_PLATFORM 覆盖)
    ds_safety_repeat_limit: int = 3                     # 单次流式响应中 <ds_safety> 连续/重复触发熔断阈值
    upstream_error_cooldown_seconds: int = 120          # 高危风控截断/安全复读后的同号短暂保护冷却期（秒）
    require_email_verification: bool = False            # C 端注册是否强制要求 6 位邮箱验证码
    resend_api_key: str = ""                            # Resend 邮件推送 API Key（主通道）
    mail_from: str = "noreply@example.com"           # 验证邮件发件人地址
    smtp_host: str = ""                                 # 备用 SMTP 主机
    smtp_port: int = 465                                # 备用 SMTP SSL 端口
    smtp_user: str = ""                                 # 备用 SMTP 账号
    smtp_password: str = ""                             # 备用 SMTP 密码

    def model_dump(self, *args, **kwargs) -> Dict[str, Any]:
        """兼容 Pydantic v1 (.dict) 与 Pydantic v2 (.model_dump)"""
        if hasattr(super(), "model_dump"):
            return super().model_dump(*args, **kwargs)  # type: ignore
        return self.dict(*args, **kwargs)


class ConfigManager:
    """线程安全的配置管理器"""

    def __init__(self, config_path: Path = CONFIG_FILE):
        self.config_path = config_path
        self._lock = threading.RLock()
        self.config = GatewayConfig()
        self.accounts: List[Dict[str, Any]] = []
        self.load()

    def load(self):
        with self._lock:
            # 1. 优先从 SQLite 数据库获取账号资产与系统配置（第一事实源）
            db_accounts = []
            db_settings = {}
            try:
                from app.db.repositories import account_repo, settings_repo
                raw_db_accs = account_repo.list_all()
                if raw_db_accs:
                    for r in raw_db_accs:
                        d = dict(r)
                        d["_email"] = d.get("email") or ""
                        d["_password"] = d.get("password") or ""
                        db_accounts.append(d)
                db_settings = settings_repo.get_all()
            except Exception as e:
                print(f"[Config] 从 SQLite 加载初始数据异常: {e}")

            # 2. 若 SQLite 已有系统配置，直接按数据库初始化配置
            if db_settings:
                try:
                    self.config = GatewayConfig(**db_settings)
                except Exception as e:
                    print(f"[Config] 解析 SQLite 系统配置失败，使用默认配置: {e}")

            # 3. 检查存量 JSON (用于首次初次导入或过渡兼容)
            if self.config_path.exists():
                try:
                    data = json.loads(self.config_path.read_text("utf-8"))
                    json_accounts = data.get("accounts", [])
                    cfg_data = {k: v for k, v in data.items() if k != "accounts"}
                    # 若 SQLite 尚未初始化配置，则继承 JSON 中的配置并写库
                    if not db_settings and cfg_data:
                        self.config = GatewayConfig(**cfg_data)
                        try:
                            from app.db.repositories import settings_repo
                            settings_repo.set_batch(self.config.model_dump())
                        except Exception:
                            pass
                    # 若 SQLite 已入库则优先使用 SQLite 数据，否则使用 JSON 初始数据并异步入库
                    if db_accounts:
                        self.accounts = db_accounts
                    else:
                        self.accounts = json_accounts
                        if json_accounts:
                            try:
                                from app.db.repositories import account_repo
                                account_repo.save_batch(json_accounts)
                            except Exception:
                                pass
                except Exception as e:
                    print(f"[Config] 加载 JSON 失败: {e}")
                    self.accounts = db_accounts
            else:
                self.accounts = db_accounts
                # 确保 SQLite 存有一份配置
                try:
                    from app.db.repositories import settings_repo
                    if not db_settings:
                        settings_repo.set_batch(self.config.model_dump())
                except Exception:
                    pass

    def save(self):
        with self._lock:
            # 1. 核心持久化：单轨强写入 SQLite
            try:
                from app.db.repositories import settings_repo, account_repo
                settings_repo.set_batch(self.config.model_dump())
                if self.accounts:
                    account_repo.save_batch(self.accounts)
            except Exception as e:
                print(f"[Config] 写入 SQLite 失败: {e}")

            # 2. 软同步/镜像：若配置路径有效则同步一份快照（便于运维排查与兼容老脚本，失败不影响主服务）
            try:
                data = self.config.model_dump()
                data["accounts"] = self.accounts
                self.config_path.parent.mkdir(parents=True, exist_ok=True)
                self.config_path.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            except Exception:
                pass

    def get_accounts(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.accounts)

    def update_account(self, label: str, **kwargs):
        with self._lock:
            now_ts = int(time.time())
            existing_acc = next((a for a in self.accounts if a.get("account_label") == label), None)
            tok_upd_val = None
            has_tok_upd_arg = "token_updated_at" in kwargs
            if has_tok_upd_arg:
                tok_upd_val = kwargs["token_updated_at"]
            elif "token" in kwargs:
                new_tok = kwargs.get("token")
                if not new_tok:
                    tok_upd_val = 0
                elif existing_acc is None or (existing_acc.get("token") or "") != new_tok or not existing_acc.get("token_updated_at"):
                    tok_upd_val = now_ts
                if tok_upd_val is not None:
                    kwargs["token_updated_at"] = tok_upd_val

            for acc in self.accounts:
                if acc.get("account_label") == label:
                    acc.update(kwargs)
                    break
            self.save()
            try:
                from app.db.repositories import account_repo, _UNSET
                repo_kw = dict(
                    account_label=label,
                    token=kwargs.get("token", _UNSET),
                    status=kwargs.get("status", _UNSET),
                    is_valid=kwargs.get("is_valid", _UNSET),
                    mute_until=kwargs.get("mute_until", _UNSET),
                    mute_count=kwargs.get("mute_count", _UNSET),
                    ban_kind=kwargs.get("ban_kind", _UNSET),
                    last_used=kwargs.get("last_used", _UNSET),
                    session_id=kwargs.get("session_id", _UNSET),
                    proxy=kwargs.get("proxy", _UNSET),
                )
                if has_tok_upd_arg or tok_upd_val is not None:
                    repo_kw["token_updated_at"] = tok_upd_val
                account_repo.update_runtime_state(**repo_kw)
            except Exception:
                pass

    def add_account(self, account_data: Dict[str, Any]):
        with self._lock:
            # 查重覆盖
            label = account_data.get("account_label")
            self.accounts = [a for a in self.accounts if a.get("account_label") != label]
            self.accounts.append(account_data)
            self.save()
            try:
                from app.db.repositories import account_repo
                account_repo.save(account_data)
            except Exception:
                pass

    def remove_account(self, label: str):
        with self._lock:
            self.accounts = [a for a in self.accounts if a.get("account_label") != label]
            self.save()
            try:
                from app.db.repositories import account_repo
                account_repo.delete(label)
            except Exception:
                pass


config_manager = ConfigManager()
