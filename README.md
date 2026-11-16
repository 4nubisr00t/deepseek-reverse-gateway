# DeepSeek Reverse Gateway — 协议分析与多协议 Agent 网关学习范本

> 一个面向 **协议逆向研究** 与 **LLM Agent 网关中间件工程** 的开源精简范本。  
> 已剔除商业计费、C 端门户与批量产号循环，保留三大核心工程资产：**单账号 P-256 受信任设备快速注册绕过**、**OpenAI / Claude / Responses 三协议 Agent 适配器**，以及 **全链路请求与掉号特征探针**。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

---

## 核心技术模块

### 1. 认证协议绕过与受信任设备签章（核心逆向范本）
- **WASM PoW 算力求解**：[pow_solver.py](app/core/pow_solver.py) + [sha3_wasm_bg.wasm](app/core/sha3_wasm_bg.wasm) 原生求解前置与对话 PoW 挑战。
- **一键单号快速注册与 5 件套铸造**：[quick_register.py](app/engine/quick_register.py) / [quick_register.py CLI](quick_register.py)
  - PoW 出口合规预检（提前拦截大陆受限 IP `biz_code=6`）；
  - 通过 HTTP 解耦对接独立打码农场 [blacklight-vlm-solver](https://github.com/4nubisr00t/blacklight-vlm-solver) 获取验证码 Token 与设备指纹；
  - RSA+AES 登录载荷加密（`x-request-encryption-key`）与 `curl_cffi` TLS 指纹伪装；
  - 内存铸造 **ECDSA P-256 (ES256)** 公私钥对，完成 `trusted_device/register` & `verify`，换发 30 天受信任设备凭证。
- **免密签章自愈续杯**：[http_auth_engine.py](app/engine/http_auth_engine.py) + [trusted_relogin_engine.py](app/engine/trusted_relogin_engine.py)
  - 当会话 Token 过期（`40003`）触发登录挑战 `biz_code=13` 时，按 `[mfa_challenge, mfa_session_id, credential_id, "verify"].join("\n")` 进行 IEEE P1363 裸签名并通过 `/mfa/verify_signature` 原地换发新 Token，无需反复邮件 OTP。

### 2. 多协议 Agent 网关中间件
- **三协议兼容入口**：
  - OpenAI Chat Completions (`/v1/chat/completions`) — [openai_routes.py](app/api/openai_routes.py)
  - Anthropic Messages API (`/v1/messages`) — [claude_routes.py](app/api/claude_routes.py)
  - OpenAI Responses API (`/v1/responses`) — [responses_routes.py](app/api/responses_routes.py)
- **流式 Tool Calling 桥接与防幻觉治理**：
  - [tool_bridge.py](app/engine/tool_bridge.py) / [streaming_parser.py](app/engine/streaming_parser.py) / [dsml_stack_parser.py](app/engine/dsml_stack_parser.py)：DSML/XML 增量流式解析、参数类型守卫、死循环熔断与格式损坏打回自愈。
  - [session_tree.py](app/engine/session_tree.py)：按对话前缀指纹复用上游会话树，支持达轮数上限自动分叉重水化。

### 3. 稳定性调度与请求级掉落探针
- **账号池与确定性代理亲和**：[account_pool.py](app/engine/account_pool.py) + [proxy_manager.py](app/engine/proxy_manager.py)（基于 `zlib.crc32` 的跨进程固定代理绑定 + 同次请求全链路单出口一致性 + 断路器退避）。
- **请求级掉落/风控探针**：[drop_probe.py](app/engine/drop_probe.py) 记录每个 Token 的请求序号 `req_seq`、`token_age_s`、出口 IP 漂移状态，并在命中 `40003` / `biz_code=14` / `401` 时自动冻结最近上下文轨迹快照供 `GET /api/system/drop_probe` 诊断。
- **精简版 Vue 3 运维后台**：[frontend/](frontend/) 保留号池监控、API Key 授权、调用统计、运维日志与「一键单号快速注册」三步引导页。

---

## 目录结构

```text
deepseek-reverse-gateway/
├── main.py                     # FastAPI 启动入口（OpenAI / Claude / Responses / Admin）
├── quick_register.py           # 单账号交互式快速注册 CLI（零批量/接码依赖）
├── app/
│   ├── config.py               # 分层配置与单模型归一收敛
│   ├── api/                    # 协议路由与运维后台 API
│   ├── core/                   # WASM PoW、JWT/Admin 鉴权、错误目录
│   ├── db/                     # SQLite Schema、Repository 与自动迁移
│   └── engine/                 # 协议客户端、P-256 签章、DSML 解析、账号池、drop_probe
├── frontend/                   # Vue 3 + Vite 运维管理后台
└── tests/                      # 核心协议、ToolBridge、快速注册与探针单元测试
```

---

## 快速开始

### 1. 安装依赖与初始化配置

```bash
pip install -r requirements.txt
cp config.example.json config.json
```

### 2. 编译运维后台（可选）

```bash
cd frontend
npm install
npm run build
cd ..
```

### 3. 启动网关服务

```bash
python main.py
```

- 运维后台：`http://127.0.0.1:8000/admin`（默认登录口令见 `config.json` 中 `admin_password`，首次部署请修改）
- OpenAI 兼容端点：`http://127.0.0.1:8000/v1/chat/completions`
- Claude 兼容端点：`http://127.0.0.1:8000/v1/messages`
- Responses 兼容端点：`http://127.0.0.1:8000/v1/responses`

### 4. 单账号快速注册（需先运行 `blacklight-vlm-solver` 或手动提供打码 Token）

```bash
python quick_register.py --email your_email@example.com --proxy http://127.0.0.1:7890
```

也可在运维后台 **「一键单号快注」** 页签中通过 3 步交互完成单个账号的 PoW 预检、OTP 校验与 ECDSA P-256 密钥铸造。

---

## 测试

```bash
pytest -v
```

---

## 免责声明

本项目定位为 **Web/App 认证协议逆向分析（PoW / 载荷加密 / P-256 设备签章）、LLM Agent 多协议适配与流式 Tool-Call 工程架构** 的学习范本。已主动移除任何批量自动注册、第三方接码平台对接及商业计费功能，请仅在合法授权与个人学习研究范围内使用。
