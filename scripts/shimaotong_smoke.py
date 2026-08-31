# -*- coding: utf-8 -*-
"""世贸通抬头报关 skill 冒烟测试 — 只读验证，不创建/提交业务订单。

覆盖：
  1. skill 包导入 + 工具注册（含惰性依赖 ddddocr/cv2 真正可加载）
  2. 验证码识别模块可实例化（ddddocr + opencv 均已安装）
  3. 真实登录世贸通（验证码 OCR + 加密密码 + session）
  4. 只读查询：获取下一个 orderNo（确认 session 可用）
  5. 自动发现链路（只读）：读跟踪表定位待办 Excel；发现为空时验证 run 缺参报错
"""

import sys

import httpx  # noqa: F401  # 仅占位，未用


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("=== 1. skill 包导入 + 工具注册 ===")
    from skills.shimaotong import get_shimaotong_tools, tool_shimaotong_submit
    tools = get_shimaotong_tools()
    assert [t.name for t in tools] == ["tool_shimaotong_submit"], tools
    print(f"  [PASS] 工具已注册: {[t.name for t in tools]}")

    print("\n=== 2. 惰性依赖加载（ddddocr + opencv + numpy）===")
    from skills.shimaotong.captcha import _require_vision, _require_ocr
    _require_vision()
    _require_ocr()
    print("  [PASS] cv2/numpy/ddddocr 均可加载")

    print("\n=== 3. 真实登录世贸通（验证码 OCR + 加密密码）===")
    from skills.shimaotong.config import shimaotong_credentials
    from skills.shimaotong.auth import login
    creds = shimaotong_credentials()
    print(f"  使用账号: {creds['username']}（密码长度 {len(creds['password'])}）")
    session, login_res = login(creds["username"], creds["password"])
    msg = login_res.get("msg", "")
    print(f"  登录响应: code={login_res.get('code')} msg={msg!r}")
    assert login_res.get("code") == 200, f"登录失败: {login_res}"
    print("  [PASS] 登录成功")

    print("\n=== 4. 只读查询：下一个 orderNo ===")
    from skills.shimaotong.api_client import fetch_next_order_no
    next_no = fetch_next_order_no(session)
    print(f"  [PASS] 下一个订单号: {next_no}")

    print("\n=== 5. 自动发现链路（只读，不触发业务）===")
    from skills.shimaotong.api_client import find_order_folders
    from skills.shimaotong.flow import run, _discover_excel_files

    folders = find_order_folders()
    print(f"  跟踪表发现合同目录: {len(folders)} 个")
    assert isinstance(folders, list), folders

    excel_files = _discover_excel_files()
    print(f"  待办报关 Excel: {len(excel_files)} 个 {excel_files[:2]}")
    assert isinstance(excel_files, list), excel_files

    # 只有发现为空时才调 run({}) 验证缺参报错；
    # 若发现到待办文件则跳过 run（否则会自动登录并创建真实订单，冒烟不允许）。
    if not excel_files:
        res = run({})
        print(f"  run({{}}) -> status={res['status']} message={res['message']!r}")
        assert res["status"] == "error", res
        print("  [PASS] 缺 excel_path 且无发现 → 干净报错")
    else:
        print("  [PASS] 发现待办 Excel，跳过 run({})（避免真实业务写入）")

    print("\n" + "=" * 60)
    print("冒烟全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
