# -*- coding: utf-8 -*-
"""验证码识别模块（惰性导入重依赖）。

验证码特征：
  - 尺寸 160×60，白色背景 + 黑色字符
  - 包含数学算式（如 5-4=?），外围有矩形边框 + 水平干扰线
  - 使用 ddddocr 做 OCR，辅以 OpenCV 像素投影检测运算符

依赖 ddddocr / opencv-python / numpy，均为惰性导入：
  只在登录真正调用时加载，skill 包加载阶段不依赖它们（agent 环境未装也不影响启动）。
识别策略：
  1. 优先用像素投影检测运算符（+/-/×）
  2. 失败则 OCR 整图，取前两个数字做减法
"""

import re
from typing import Optional


def _require_vision():
    """惰性导入 cv2/numpy，缺失时给出明确安装指引。"""
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "世贸通验证码识别需要 opencv-python + numpy，请先安装："
            "pip install opencv-python numpy ddddocr"
        ) from e
    return cv2, numpy


def _require_ocr():
    """惰性导入 ddddocr，缺失时给出明确安装指引。"""
    try:
        import ddddocr  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "世贸通验证码识别需要 ddddocr，请先安装：pip install ddddocr"
        ) from e
    return ddddocr


def solve(image_bytes: bytes) -> int:
    """
    解析验证码图片，返回算式计算结果。

    Args:
        image_bytes: 验证码图片的原始字节数据（PNG/JPEG）

    Returns:
        算式结果，如 5-4=? 返回 1

    Raises:
        RuntimeError: 无法从图片中提取有效数字，或依赖未安装
    """
    # 优先像素检测
    result = _pixel_detect(image_bytes)
    if result is not None:
        a, b, operator = result
        if operator == "+":
            return a + b
        elif operator == "*":
            return a * b
        return a - b

    # OCR 降级
    return _ocr_fallback(image_bytes)


def ocr_text(image_bytes: bytes) -> str:
    """对验证码图片做纯 OCR，返回识别文本。"""
    ddddocr = _require_ocr()
    ocr = ddddocr.DdddOcr(beta=True, show_ad=False)
    return ocr.classification(image_bytes).replace(" ", "")


def extract_digits(ocr_text: str) -> list[int]:
    """从 OCR 文本中提取所有数字。"""
    return [int(d) for d in re.findall(r"\d", ocr_text)]


def has_operator(ocr_text: str) -> Optional[str]:
    """
    检查 OCR 文本是否包含明确运算符。

    Returns:
        "+", "-", "*" 之一，或 None
    """
    if "+" in ocr_text:
        return "+"
    if "-" in ocr_text:
        return "-"
    if "x" in ocr_text.lower() or "*" in ocr_text:
        return "*"
    return None


# ---- 内部实现 ----

def _ocr_fallback(image_bytes: bytes) -> int:
    """纯 OCR 降级方案：取前两个数字做减法。"""
    text = ocr_text(image_bytes)
    digits = extract_digits(text)
    if len(digits) >= 2:
        return digits[0] - digits[1]
    raise RuntimeError(f"无法解析验证码: {text!r}")


def _pixel_detect(image_bytes: bytes) -> Optional[tuple]:
    """
    像素投影检测运算符和操作数。

    步骤：
      1. OTSU 二值化
      2. 裁掉边框（行方差法区分边框行和内容行）
      3. 垂直投影找低像素区（运算符候选）
      4. 在运算符内部判断 +（中心列高）还是 ×（像素断层）
      5. 左右找数字区域并 OCR
    """
    cv2, numpy = _require_vision()
    ddddocr = _require_ocr()

    arr = numpy.frombuffer(image_bytes, numpy.uint8)
    gray = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    h, w = th.shape

    # ---- 裁剪边框 ----
    row_vars = numpy.array([th[y].var() for y in range(h)])
    content_rows = numpy.where((th // 255).sum(axis=1) > 5)[0]
    if len(content_rows) < 10:
        return None
    content_rows = content_rows[row_vars[content_rows] > 100]
    if len(content_rows) < 10:
        return None
    top = max(0, content_rows[0] - 2)
    bottom = min(h, content_rows[-1] + 3)
    th = th[top:bottom, :]
    gray = gray[top:bottom, :]

    col_sums = (th // 255).sum(axis=0)
    content_cols = numpy.where(col_sums > 3)[0]
    if len(content_cols) < 20:
        return None
    th = th[:, content_cols[0]:content_cols[-1] + 1]
    gray = gray[:, content_cols[0]:content_cols[-1] + 1]
    h2, w2 = th.shape
    col_sums2 = (th // 255).sum(axis=0)

    # ---- 找运算符候选 ----
    segments = _find_low_pixel_segments(col_sums2, w2)
    if not segments:
        return None

    ocr = ddddocr.DdddOcr(beta=True, show_ad=False)

    for x1, x2 in segments:
        # 确认前后有数字存在
        before = col_sums2[max(0, x1 - 12):x1].max() if x1 > 12 else 0
        after = col_sums2[x2 + 1:min(w2, x2 + 13)].max() if x2 < w2 - 1 else 0
        if before < 10 or after < 10:
            continue

        # 判断运算符类型（只看中间 60% 区域，排除上下干扰）
        operator = _classify_operator(th, x1, x2, h2)
        if operator is None:
            continue

        # 找左右数字区域并 OCR
        a = _ocr_region(gray, col_sums2, ocr, x1, x2, w2, side="left")
        b = _ocr_region(gray, col_sums2, ocr, x1, x2, w2, side="right")

        if a is not None and b is not None:
            return a, b, operator

    return None


def _find_low_pixel_segments(col_sums, w: int) -> list:
    """在垂直投影中找连续低像素区段（运算符候选）。"""
    low_cols = [x for x in range(1, w - 1) if 2 <= col_sums[x] <= 12]
    if not low_cols:
        return []

    segments = []
    seg_start = low_cols[0]
    for i in range(1, len(low_cols)):
        if low_cols[i] - low_cols[i - 1] > 2:
            if low_cols[i - 1] - seg_start >= 4:
                segments.append((seg_start, low_cols[i - 1]))
            seg_start = low_cols[i]
    if low_cols[-1] - seg_start >= 4:
        segments.append((seg_start, low_cols[-1]))
    return [(s, e) for s, e in segments if 5 < (e - s) < 25]


def _classify_operator(th, x1: int, x2: int, h: int) -> Optional[str]:
    """
    判断运算符类型。

    Args:
        th: 二值化图像
        x1, x2: 运算符水平范围
        h: 图像高度

    Returns:
        "+" / "-" / "*" / None
    """
    # 只看中间 60% 垂直区域
    op_region = th[h // 5:4 * h // 5, x1:x2 + 1]
    col_heights = (op_region // 255).sum(axis=0)
    if len(col_heights) < 3:
        return None

    avg_h = col_heights.mean()
    mid = len(col_heights) // 2
    mid_h = col_heights[mid]

    # + : 中心列显著高（十字形有垂直笔画）
    if mid_h > avg_h * 1.6 and mid_h > 6:
        return "+"

    # × : 某列有像素断层（两条对角线产生两段黑像素）
    for cx in range(op_region.shape[1]):
        col_data = op_region[:, cx]
        transitions = sum(
            1 for py in range(1, len(col_data))
            if (col_data[py] > 0) != (col_data[py - 1] > 0)
        )
        if transitions >= 4:
            return "*"

    return "-"


def _ocr_region(gray, col_sums, ocr, op_x1: int, op_x2: int, w: int, side: str) -> Optional[int]:
    """
    在运算符左侧或右侧识别单个数字。

    Args:
        gray: 灰度图像
        col_sums: 每列黑像素数
        ocr: ddddocr 实例
        op_x1, op_x2: 运算符范围
        w: 图像宽度
        side: "left" 或 "right"

    Returns:
        识别的数字，或 None
    """
    cv2, numpy = _require_vision()

    # 找数字列范围
    if side == "left":
        cols = []
        for x in range(op_x1 - 1, 0, -1):
            if col_sums[x] > 8:
                cols.append(x)
            elif cols:
                break
    else:
        cols = []
        for x in range(op_x2 + 1, w):
            if col_sums[x] > 8:
                cols.append(x)
            elif cols:
                break

    if not cols:
        return None

    rx1, rx2 = min(cols), max(cols)
    px1 = max(0, rx1 - 2)
    px2 = min(w - 1, rx2 + 2)
    roi = gray[:, px1:px2 + 1]
    roi = cv2.resize(roi, (40, 50), interpolation=cv2.INTER_CUBIC)
    _, buf = cv2.imencode(".png", roi)

    text = ocr.classification(buf.tobytes()).strip()
    m = re.search(r"\d+", text)
    return int(m.group()[0]) if m else None
