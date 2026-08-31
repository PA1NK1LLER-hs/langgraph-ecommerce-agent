# -*- coding: utf-8 -*-
"""登录认证模块。

流程：
  1. 调 /encryptPassword 加密密码
  2. 获取验证码图片 → OCR 识别（内存中完成，不落盘）
  3. 用识别结果尝试登录
  4. 失败则自动重试（最多 10 次）

验证码策略：
  - OCR 含明确运算符 → 直接使用
  - 否则对同张图试全部候选（-, +, *）
"""

import logging

import requests

from .config import (
    BASE_URL, HEADERS,
    ENDPOINT_ENCRYPT_PASSWORD, ENDPOINT_LOGIN, ENDPOINT_LOGIN_PAGE, ENDPOINT_CAPTCHA,
)
from .captcha import ocr_text, extract_digits, has_operator

logger = logging.getLogger(__name__)


def encrypt_password(plain_password: str) -> str:
    """
    调用加密接口，将明文密码转为密文。

    Args:
        plain_password: 明文密码

    Returns:
        加密后的密码字符串

    Raises:
        requests.RequestException: 网络异常时
    """
    resp = requests.post(
        f"{BASE_URL}{ENDPOINT_ENCRYPT_PASSWORD}",
        data={"password": plain_password},
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("msg") or data.get("data") or ""


def login(username: str, plain_password: str, max_retries: int = 10) -> tuple[requests.Session, dict]:
    """
    登录世贸通，自动处理验证码。

    每轮重试：
      1. 访问登录页获取 session cookie
      2. 请求验证码图片（bytes 存内存，不写文件）
      3. OCR 提取数字 + 推断运算符
      4. 提交登录

    Args:
        username:       登录用户名
        plain_password: 明文密码
        max_retries:    最大重试次数

    Returns:
        (requests.Session, 登录响应 dict)

    Raises:
        RuntimeError: 超过最大重试次数仍未成功
    """
    enc_pwd = encrypt_password(plain_password)

    for attempt in range(max_retries):
        logger.debug("第 %d/%d 次登录尝试", attempt + 1, max_retries)

        # 新 session，访问登录页拿 cookie
        session = requests.Session()
        session.get(f"{BASE_URL}{ENDPOINT_LOGIN_PAGE}", headers=HEADERS, timeout=10)

        # 获取验证码
        resp_img = session.get(
            f"{BASE_URL}{ENDPOINT_CAPTCHA}",
            headers=HEADERS,
            timeout=10,
        )
        img_bytes = resp_img.content

        # OCR 提取数字
        text = ocr_text(img_bytes)
        logger.debug("验证码 OCR: %r", text)

        digits = extract_digits(text)
        if len(digits) < 2:
            continue

        a, b = digits[0], digits[1]

        # 确定候选答案
        op = has_operator(text)
        if op == "+":
            candidates = [a + b]
        elif op == "-":
            candidates = [a - b]
        elif op == "*":
            candidates = [a * b]
        else:
            candidates = [a - b, a + b, a * b]
            # 去重
            seen: set[int] = set()
            candidates = [c for c in candidates if not (c in seen or seen.add(c))]

        # 逐个尝试
        for answer in candidates:
            resp = session.post(
                f"{BASE_URL}{ENDPOINT_LOGIN}",
                data={
                    "username": username,
                    "password": enc_pwd,
                    "validateCode": str(answer),
                    "rememberMe": "false",
                },
                headers=HEADERS,
                timeout=30,
            )
            resp.raise_for_status()
            result = resp.json()
            if result.get("code") == 200:
                logger.info("世贸通登录成功")
                return session, result
            logger.debug("验证码答案 %s 失败: %s", answer, result.get("msg"))

    raise RuntimeError(f"世贸通登录失败，已重试 {max_retries} 次")
