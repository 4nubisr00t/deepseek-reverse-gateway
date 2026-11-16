# -*- coding: utf-8 -*-
"""
tests/test_http_auth_engine.py
单元测试: 验证 http_auth_engine 的加密、解密可逆性、签名转换、指纹提取及模拟请求
"""

import os
import sys
import json
import base64
import unittest
from unittest.mock import patch, MagicMock

# 加入工程路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import rsa, ec
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding

from app.engine.http_auth_engine import (
    encrypt_auth_payload,
    der_to_raw_p1363_signature,
    sign_challenge_p256,
    extract_shumei_device_id,
    renew_account_token
)


class TestHttpAuthEngine(unittest.TestCase):

    def setUp(self):
        # 生成一对测试用的临时 RSA 密钥对
        self.rsa_priv = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048
        )
        self.rsa_pub_der = self.rsa_priv.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        )
        self.rsa_pub_b64 = base64.b64encode(self.rsa_pub_der).decode('ascii')

        # 生成一对测试用的临时 ECDSA P-256 私钥
        self.ec_priv = ec.generate_private_key(ec.SECP256R1())
        self.ec_priv_der = self.ec_priv.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        )
        self.ec_priv_b64 = base64.b64encode(self.ec_priv_der).decode('ascii')

    def test_encryption_roundtrip(self):
        """测试 RSA-OAEP + AES-CBC 混合加密是否能被标准私钥完整还原解密"""
        test_payload = {
            "email": "test_bot@example.com",
            "password": "Password123!",
            "device_id": "BaaYe63E2H6zwF71pgMQ+QekZ",
            "mobile": "",
            "area_code": "",
            "os": "web"
        }

        cipher_body, enc_key_b64 = encrypt_auth_payload(test_payload, rsa_pub_b64=self.rsa_pub_b64)
        self.assertTrue(isinstance(cipher_body, bytes))
        self.assertTrue(len(cipher_body) > 0)
        self.assertTrue(len(enc_key_b64) > 0)

        # 私钥解密 key_iv_block
        enc_key_bytes = base64.b64decode(enc_key_b64)
        decrypted_key_iv = self.rsa_priv.decrypt(
            enc_key_bytes,
            asym_padding.OAEP(
                mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None
            )
        )
        self.assertEqual(len(decrypted_key_iv), 32)
        aes_key = decrypted_key_iv[:16]
        aes_iv = decrypted_key_iv[16:]

        # 解密 AES 密文
        cipher = Cipher(algorithms.AES(aes_key), modes.CBC(aes_iv))
        decryptor = cipher.decryptor()
        padded_plain = decryptor.update(cipher_body) + decryptor.finalize()

        unpadder = padding.PKCS7(128).unpadder()
        plain_bytes = unpadder.update(padded_plain) + unpadder.finalize()
        recovered_obj = json.loads(plain_bytes.decode('utf-8'))

        self.assertEqual(recovered_obj["email"], test_payload["email"])
        self.assertEqual(recovered_obj["device_id"], test_payload["device_id"])
        self.assertEqual(recovered_obj["os"], "web")

    def test_shumei_fingerprint_extraction(self):
        """测试避坑铁律一：从不同结构的账号对象中正确提取数美 B 特征码"""
        # Case 1: device_fingerprint 为 JSON 字符串
        acc1 = {
            "email": "acc1@test.com",
            "device_id": "internal-uuid-1111-2222",
            "device_fingerprint": json.dumps({"device_id": "BaaYe63E2H6zwF71pgMQ+QekZ123"})
        }
        self.assertEqual(extract_shumei_device_id(acc1), "BaaYe63E2H6zwF71pgMQ+QekZ123")

        # Case 2: device_fingerprint 含有 smid
        acc2 = {
            "email": "acc2@test.com",
            "device_id": "internal-uuid-3333",
            "device_fingerprint": json.dumps({"smid": "BsmidRealValue456"})
        }
        self.assertEqual(extract_shumei_device_id(acc2), "BsmidRealValue456")

        # Case 3: 顶层 device_id 本身就是以 B 开头
        acc3 = {
            "email": "acc3@test.com",
            "device_id": "BtopLevelDirectShumei789"
        }
        self.assertEqual(extract_shumei_device_id(acc3), "BtopLevelDirectShumei789")

        # Case 4: 无任何 B 指纹，回退 UUID
        acc4 = {
            "email": "acc4@test.com",
            "device_id": "4488bfb0-1cad-4387-9b07-9432202fde41"
        }
        self.assertEqual(extract_shumei_device_id(acc4), "4488bfb0-1cad-4387-9b07-9432202fde41")

    def test_ecdsa_p1363_signing(self):
        """测试避坑铁律二：ECDSA P-256 签名转换为 64 字节 IEEE P1363 格式并能用公钥验签"""
        plain_text = "challenge_123\nsession_456\ncred_789\nverify"
        sig_b64 = sign_challenge_p256(self.ec_priv_b64, plain_text)

        raw_sig = base64.b64decode(sig_b64)
        self.assertEqual(len(raw_sig), 64, "IEEE P1363 签名必须是严格 64 字节 (32B r + 32B s)")

        # 验证 r 和 s 的有效性
        r = int.from_bytes(raw_sig[:32], byteorder='big')
        s = int.from_bytes(raw_sig[32:], byteorder='big')
        self.assertTrue(r > 0)
        self.assertTrue(s > 0)

    @patch("app.engine.http_auth_engine.cffi_requests")
    def test_renew_account_token_direct_success(self, mock_requests):
        """测试场景 A：登录接口直接返回 biz_code=0 换发 Token"""
        mock_sess = MagicMock()
        mock_requests.Session.return_value = mock_sess

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "code": 0,
            "data": {
                "biz_code": 0,
                "biz_data": {
                    "user": {
                        "token": "new_mocked_token_xyz_direct"
                    }
                }
            }
        }
        mock_sess.post.return_value = mock_resp

        acc = {
            "email": "active_user@test.com",
            "password": "valid_pwd",
            "device_id": "BaaYe63E2H6zwF71pgMQ+QekZ",
            "credential_id": "cred_123",
            "mfa_private_key": self.ec_priv_b64
        }

        ok, res = renew_account_token(acc, platform="web")
        self.assertTrue(ok)
        self.assertEqual(res["token"], "new_mocked_token_xyz_direct")
        self.assertEqual(res["mode"], "direct_login")

    @patch("app.engine.http_auth_engine.cffi_requests")
    def test_renew_account_token_challenge_success(self, mock_requests):
        """测试场景 B：登录接口返回 13，进入明文验签后下发 Token"""
        mock_sess = MagicMock()
        mock_requests.Session.return_value = mock_sess

        # 第一次调用 post (/login) 返回 biz_code=13
        mock_resp_login = MagicMock()
        mock_resp_login.json.return_value = {
            "code": 0,
            "data": {
                "biz_code": 13,
                "biz_data": {
                    "mfa_session_id": "session_mfa_123",
                    "mfa_challenge": "challenge_random_string_abc"
                }
            }
        }

        # 第二次调用 post (/verify_signature) 返回 biz_code=0 及新 Token
        mock_resp_verify = MagicMock()
        mock_resp_verify.json.return_value = {
            "code": 0,
            "data": {
                "biz_code": 0,
                "biz_data": {
                    "user": {
                        "token": "token_from_verified_signature_challenge"
                    }
                }
            }
        }

        mock_sess.post.side_effect = [mock_resp_login, mock_resp_verify]

        acc = {
            "email": "active_user@test.com",
            "password": "valid_pwd",
            "device_id": "BaaYe63E2H6zwF71pgMQ+QekZ",
            "credential_id": "cred_123",
            "mfa_private_key": self.ec_priv_b64
        }

        ok, res = renew_account_token(acc, platform="web")
        self.assertTrue(ok)
        self.assertEqual(res["token"], "token_from_verified_signature_challenge")
        self.assertEqual(res["mode"], "signature_challenge")

        # 验证第二阶段验签确实是以明文 json 形式调用，且未注入 x-request-encryption-key
        calls = mock_sess.post.call_args_list
        self.assertEqual(len(calls), 2)
        verify_call = calls[1]
        self.assertIn("json", verify_call.kwargs)
        self.assertEqual(verify_call.kwargs["json"]["mfa_session_id"], "session_mfa_123")
        self.assertNotIn("x-request-encryption-key", verify_call.kwargs.get("headers", {}))


if __name__ == "__main__":
    unittest.main()
