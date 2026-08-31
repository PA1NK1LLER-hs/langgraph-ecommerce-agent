# -*- coding: utf-8 -*-
"""世贸通抬头报关统一配置 — 全部从环境变量（.env）读取，不在代码里硬编码凭证/路径。

与 `skills/rpa/common/config.py` 同约定：单一来源是项目根 .env（`src/config.py` 顶层 `load_dotenv` 统一加载），
本模块只做 `os.getenv` + 默认值 + 惰性校验（凭证到真正调用时才报错）。
"""

import os

# ── 服务器 ──
BASE_URL = os.getenv("SHIMAOTONG_BASE_URL", "https://work.shimaotong.com")

# ── HTTP 请求头 ──
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": f"{BASE_URL}/login",
    "Origin": BASE_URL,
    "X-Requested-With": "XMLHttpRequest",
}

# ── API 端点 ──
ENDPOINT_ENCRYPT_PASSWORD = "/encryptPassword"
ENDPOINT_LOGIN = "/login"
ENDPOINT_LOGIN_PAGE = "/login"
ENDPOINT_CAPTCHA = "/captcha/captchaImage?type=math"
ENDPOINT_SKU_AUTOCOMPLETE = "/api/autocomplete/customer/goods/sku"
ENDPOINT_ORDER_LIST = "/api/business/order/list"
ENDPOINT_ORDER_SAVE = "/api/business/order/save"
ENDPOINT_DOC_ORDER_LIST = "/api/business/order/list"
ENDPOINT_ORDER_GET_INFO = "/api/business/order/get/info"
ENDPOINT_ORDER_SUBMIT_CHECK = "/api/business/order/submit/check"
ENDPOINT_ORDER_SUBMIT = "/api/business/order/submit"
ENDPOINT_CUSTOMS_DOWNLOAD = "/api/business/order/customs/download"
ENDPOINT_FILE_DOWNLOAD = "/file/static/download"  # 实际文件下载路径前缀（已废弃）

# ── 订单列表查询参数 ──
ORDER_LIST_PARAMS = {
    "pageSize": "10",
    "pageNum": "1",
    "orderByColumn": "createTime",
    "isAsc": "desc",
    "searchType": "1",
    "orderNo": "",
    "contractNo": "",
    "consignee": "",
    "ldNo": "",
    "dcoeStatus": "",
    "customsNoStatus": "",
    "balanceStatus": "",
    "supplierName": "",
    "hsCode": "",
    "goodsName": "",
    "auditBeginTime": "",
    "auditEndTime": "",
    "beginTime": "",
    "endTime": "",
}

# ── 订单号前缀 ──
ORDER_NO_PREFIX = "WT26SHUY"


def shimaotong_credentials() -> dict:
    """惰性校验：仅在世贸通工具实际调用时才要求凭证已设置。"""
    username = os.getenv("SHIMAOTONG_USERNAME")
    password = os.getenv("SHIMAOTONG_PASSWORD")
    if not username or not password:
        raise RuntimeError("SHIMAOTONG_USERNAME / SHIMAOTONG_PASSWORD 未设置，世贸通抬头报关无法运行")
    return {"username": username, "password": password}


# ── 订单默认值 ──
def _order_defaults() -> dict:
    """订单默认值；userId 从 .env 读取（shuyi 账号的 userId）。"""
    return {
        "status": "",
        "userId": os.getenv("SHIMAOTONG_USER_ID", ""),
        "orderNo": "",
        "contractNo": "",
        "contact": "钟女士",
        "tel": "18067427902",
        "mobile": "18067427902",
        "payMode": "T/T",              # 电汇
        "priceTerms": "CIF",
        "tradeMode": "0110",            # 一般贸易
        "transportMode": "BY SEA",
        "insName": "CONVERING ALL RISKS",
        "insRate": "0",
        "currency": "USD",
        "foType": "1",
        "lf": "1",
    }


def get_order_defaults() -> dict:
    """返回订单默认字段 dict。"""
    return _order_defaults()


# ── 订单中固定为空的字段（不包含从 Excel 动态读取的字段）──
ORDER_EMPTY_FIELDS = {
    "deliveryDate": "", "owName": "", "owAdd": "",
    "payMode": "", "lcNo": "", "shipmentDate": "",
    "transportMode": "", "otherFee": "",
    "insName": "", "insRate": "", "insAdd": "",
    "transPort": "", "marks": "",
    "cbName": "", "cbNo": "", "ldNo": "",
    "scName": "", "foName": "", "foContact": "",
    "foTel": "", "foFax": "", "foAdd": "",
    "fclOne": "", "fclTwo": "", "fclThree": "",
    "mixed": "", "noticeUrl": "", "examineImg": "",
}

# ── 中文→英文 港口/国家映射（Excel 中为中文，接口需大写英文）──
CN_TO_EN = {
    # ---- 起运港 ----
    "上海": "SHANGHAI",
    "宁波": "NINGBO",
    "深圳": "SHENZHEN",
    "青岛": "QINGDAO",
    "广州": "GUANGZHOU",
    "天津": "TIANJIN",
    "厦门": "XIAMEN",

    # ---- 美国目的港 ----
    "纽约": "NEW YORK,NY",
    "洛杉矶": "LOS ANGELES",
    "长滩": "LONG BEACH",
    "奥克兰": "OAKLAND",
    "西雅图": "SEATTLE",
    "休斯顿": "HOUSTON,TX",
    "萨凡纳": "SAVANNAH,GA",
    "迈阿密": "MIAMI,FL",
    "芝加哥": "CHICAGO,IL",
    "诺福克": "NORFOLK,VA",
    "查尔斯顿": "CHARLESTON,SC",
    "塔科马": "TACOMA,WA",
    "波士顿": "BOSTON,MA",
    "巴尔的摩": "BALTIMORE,MD",
    "杰克逊维尔": "JACKSONVILLE,FL",

    # ---- 加拿大目的港 ----
    "温哥华": "VANCOUVER",
    "多伦多": "TORONTO",
    "蒙特利尔": "MONTREAL",
    "鲁珀特王子港": "PRINCE RUPERT",
    "哈利法克斯": "HALIFAX",
    "卡尔加里": "CALGARY",

    # ---- 英国目的港 ----
    "伦敦": "LONDON",
    "弗利克斯托": "FELIXSTOWE",
    "南安普顿": "SOUTHAMPTON",
    "利物浦": "LIVERPOOL",
    "曼彻斯特": "MANCHESTER",
    "蒂尔伯里": "TILBURY",
    "格里姆斯比": "GRIMSBY",
    "布里斯托尔": "BRISTOL",

    # ---- 德国目的港 ----
    "汉堡": "HAMBURG",
    "鹿特丹": "ROTTERDAM",
    "不来梅": "BREMEN",
    "不来梅港": "BREMERHAVEN",
    "威廉港": "WILHELMSHAVEN",
    "罗斯托克": "ROSTOCK",
    "杜伊斯堡": "DUISBURG",

    # ---- 澳大利亚目的港 ----
    "悉尼": "SYDNEY",
    "墨尔本": "MELBOURNE",
    "布里斯班": "BRISBANE",

    # ---- 法国目的港 ----
    "勒阿弗尔": "LE HAVRE",
    "马赛": "MARSEILLE",
    "敦刻尔克": "DUNKIRK",

    # ---- 国家 ----
    "美国": "UNITED STATES",
    "中国": "CHINA",
    "日本": "JAPAN",
    "韩国": "KOREA",
    "德国": "GERMANY",
    "英国": "UNITED KINGDOM",
    "法国": "FRANCE",
    "加拿大": "CANADA",
    "澳大利亚": "AUSTRALIA",
    "澳洲": "AUSTRALIA",
}


def translate(value: str) -> str:
    """
    将中文地名转为规范大写英文格式。
    查表转换；未命中则原样返回并转大写。
    """
    value = value.strip()
    # 精确匹配
    if value in CN_TO_EN:
        return CN_TO_EN[value]
    # 模糊匹配（如 "纽约(上海)" → 提取中文）
    for cn, en in CN_TO_EN.items():
        if cn in value:
            return en
    # 兜底：转大写
    return value.upper()


# ---- Excel 列名映射（明细6 sheet，按列名读取，列顺序变化不影响） ----
COL_MAP = {
    "order_no":        "编号",
    "agent":           "代理",
    "country":         "国家",
    "dest_port":       "目的港",
    "origin_port":     "起运地",
    "export_company":  "出口抬头",
    "freight":         "预估海运费",
    "ins_fee":         "保险费",
    "ins_amount":      "投保金额",
    "bl_company":      "提单抬头人",
    "warehouse_no":    "进仓单号",
    "customs_no":      "报关单号",
    "fba_id":          "FBA编号",
    "warehouse_addr":  "仓库",
    "outer_sku":       "外箱SKU",
    "store_sku":       "店铺SKU",
    "product_name":    "品名",
    "huafei_sku":      "华飞系统型号SKU",
    "total_qty":       "合计数量",
    "qty":             "数量",
    "unit_price_pur":  "单价",
    "freight_per_item": "运费",
    "total_price_pur": "价格合计",
    "unit_price_export": "报关出口单价",
    "total_price_export": "报关出口价格合计",
}

# forward-fill 的列名（合并单元格区域，订单级元数据）
FFILL_COL_NAMES = [
    "编号", "代理", "国家", "目的港", "起运地", "出口抬头",
    "预估海运费", "保险费", "投保金额", "提单抬头人",
    "进仓单号", "报关单号", "FBA编号", "仓库",
]


def resolve_column_names(df_columns: list[str]) -> dict:
    """
    根据实际 Excel 列名匹配 COL_MAP，返回 {逻辑名: 实际列名}。
    对可能含年份前缀的列（如"2025年外箱SKU"）做模糊匹配。
    """
    result = {}
    for logical, expected in COL_MAP.items():
        # 精确匹配
        if expected in df_columns:
            result[logical] = expected
            continue
        # 模糊匹配（处理 "2025年外箱SKU" 这类带年份前缀的列）
        for col in df_columns:
            if expected in str(col):
                result[logical] = col
                break
    return result
