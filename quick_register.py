# -*- coding: utf-8 -*-
"""单账号一键快速注册 CLI（零批量接码依赖，交互输入邮箱验证码）。

前置：请先启动 blacklight-vlm-solver (默认 http://127.0.0.1:9527)，或手动传入打码 Token。
"""

import argparse
import json
import sys

from app.engine.quick_register import DEFAULT_FARM_SOLVE_URL, quick_register_engine


def main() -> int:
    ap = argparse.ArgumentParser(description="DeepSeek 单账号快速注册与 ECDSA P-256 受信任凭证铸造")
    ap.add_argument("--email", required=True, help="接收验证码的个人邮箱")
    ap.add_argument("--password", default="", help="登录密码（留空则自动生成强随机密码）")
    ap.add_argument("--proxy", default="", help="海外代理地址，例如 http://127.0.0.1:7890")
    ap.add_argument("--farm-url", default=DEFAULT_FARM_SOLVE_URL, help="解耦打码农场地址")
    ap.add_argument("--skip-preflight", action="store_true", help="跳过出口合规 PoW 预检")
    args = ap.parse_args()

    print("[1/3] 启动出口预检并向打码农场申请第 1 次注册凭据...")
    job = quick_register_engine.start(
        email=args.email,
        password=args.password,
        proxy=args.proxy,
        farm_url=args.farm_url,
        skip_preflight=args.skip_preflight,
    )
    job_id = job["job_id"]
    print(f"注册验证码已发送至 {job['email']} (job_id={job_id})")

    code1 = input("请输入第 1 封邮件收到的 6 位【注册验证码】: ").strip()
    print("[2/3] 提交注册并触发受信任设备二次确权发码...")
    quick_register_engine.verify_register(job_id=job_id, register_otp=code1)

    code2 = input("请输入第 2 封邮件收到的 6 位【二次确权验证码】: ").strip()
    print("[3/3] 铸造 ECDSA P-256 密钥对、签名换取 30 天签证并自检...")
    done = quick_register_engine.complete(job_id=job_id, mfa_otp=code2)
    print("\n注册成功！五件套已入池:")
    print(json.dumps(done.get("account_summary"), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
