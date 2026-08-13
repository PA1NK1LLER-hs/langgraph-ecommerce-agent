"""增量导入脚本 — 只导入之前因欠费失败的剩余产品。
运行方式: python scripts/import_products/import_remaining.py
"""

import asyncio
import logging
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from rag.indexer import get_indexer  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("import_remaining")
for _name in ("lightrag", "neo4j", "httpx", "httpcore", "openai"):
    logging.getLogger(_name).setLevel(logging.WARNING)

EXCEL_PATH = r"C:\Users\Administrator\Desktop\巧逗豆店铺各站点产品明细总表.xlsx"


async def main():
    df = pd.read_excel(EXCEL_PATH)
    cols = list(df.columns)
    site_c, sku_c, asin_c, name_c, delivery_c = cols[0], cols[1], cols[2], cols[3], cols[4]

    # 聚合 SKU 信息
    sku_info: dict[str, list[tuple[str, str, str, str]]] = {}
    for _, row in df.iterrows():
        sku = str(row[sku_c]).strip()
        site = str(row[site_c]).strip()
        asin = str(row[asin_c]).strip()
        name = str(row[name_c]).strip()
        delivery = str(row[delivery_c]).strip() if pd.notna(row[delivery_c]) else "自配送"
        sku_info.setdefault(sku, []).append((site, asin, name, delivery))

    # 找出未入库的 SKU
    idx = get_indexer()
    await idx._ensure_initialized_async()
    from lightrag.base import DocStatus
    docs = await idx._rag.doc_status.get_docs_by_statuses([DocStatus.PROCESSED])
    done_skus = {
        d.file_path.replace("qiaodoudou:", "")
        for d in docs.values()
        if getattr(d, "file_path", "").startswith("qiaodoudou:")
    }
    pending = [(s, e) for s, e in sku_info.items() if s not in done_skus]
    logger.info("Total SKUs: %d, already done: %d, pending: %d",
                len(sku_info), len(done_skus), len(pending))

    if not pending:
        logger.info("All SKUs are already imported. Nothing to do.")
        return

    # 批量索引（限并发）
    sem = asyncio.Semaphore(3)
    success = 0
    fail = 0
    total = len(pending)

    async def _index_one(sku: str, entries):
        nonlocal success, fail
        async with sem:
            parts = [f"SKU: {sku}"]
            for site, asin, name, delivery in entries:
                parts.append(f"站点{site} | ASIN: {asin} | 品名: {name} | 配送方式: {delivery}")
            text = "\n".join(parts)
            try:
                await idx._rag.ainsert(text, file_paths=[f"qiaodoudou:{sku}"])
                success += 1
                logger.info("[%d/%d] OK: %s", success + fail, total, sku)
            except Exception as exc:
                fail += 1
                logger.warning("[%d/%d] FAIL %s: %s", success + fail, total, sku, exc)

    tasks = [_index_one(sku, entries) for sku, entries in pending]
    await asyncio.gather(*tasks, return_exceptions=True)

    logger.info("Done: %d success, %d fail, %d total", success, fail, total)

    # 验证
    await idx._ensure_initialized_async()
    docs2 = await idx._rag.doc_status.get_docs_by_statuses([DocStatus.PROCESSED])
    new_done = sum(1 for d in docs2.values() if getattr(d, "file_path", "").startswith("qiaodoudou:"))
    logger.info("Verification: %d product SKUs now indexed (was %d)", new_done, len(done_skus))


if __name__ == "__main__":
    asyncio.run(main())
