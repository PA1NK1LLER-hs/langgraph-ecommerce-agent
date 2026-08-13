r"""将巧逗豆店铺产品明细总表导入知识图谱（Neo4j）和向量数据库（Qdrant）。

知识图谱结构:
    巧逗豆 (Company) --HAS_SITE--> 站点 (Site) --LISTS_PRODUCT--> 商品 (Product)

Product 节点属性: sku, asin, name, delivery_method, sites (站点列表)
同一商品挂多个站点时，与每个站点都建立 LISTS_PRODUCT 关系。

运行方式:
    python scripts/import_products/import_products.py
"""

import asyncio
import logging
import sys
from pathlib import Path

import pandas as pd
from neo4j import GraphDatabase

# 将项目 src 加入 Python 路径
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from rag.indexer import get_indexer  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("import_products")

# 降噪
for _name in ("lightrag", "neo4j", "httpx", "httpcore", "openai"):
    logging.getLogger(_name).setLevel(logging.WARNING)

EXCEL_PATH = r"C:\Users\Administrator\Desktop\巧逗豆店铺各站点产品明细总表.xlsx"

# Neo4j 配置（与项目 .env 一致）
NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "password123"

COMPANY_NAME = "巧逗豆"


# ── Neo4j 知识图谱构建 ──

async def build_knowledge_graph(df: pd.DataFrame) -> dict:
    """在 Neo4j 中构建结构化知识图谱。

    Returns:
        {sites: int, products: int, relations: int}
    """
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

    try:
        with driver.session() as session:

            # 创建公司节点
            session.run(
                """
                MERGE (c:Company {name: $name})
                RETURN c
                """,
                name=COMPANY_NAME,
            )

            # 创建所有站点节点 + HAS_SITE 关系
            sites = df.iloc[:, 0].unique().tolist()
            session.run(
                """
                UNWIND $sites AS site_name
                MERGE (s:Site {name: site_name})
                WITH s
                MATCH (c:Company {name: $company})
                MERGE (c)-[:HAS_SITE]->(s)
                """,
                sites=sites,
                company=COMPANY_NAME,
            )

            # 创建商品节点 + LISTS_PRODUCT 关系（按 SKU 分组聚合站点）
            sku_data: dict[str, dict] = {}
            cols = list(df.columns)
            site_c, sku_c, asin_c, name_c, delivery_c = cols[0], cols[1], cols[2], cols[3], cols[4]

            for _, row in df.iterrows():
                sku = str(row[sku_c]).strip()
                site = str(row[site_c]).strip()
                asin = str(row[asin_c]).strip()
                name = str(row[name_c]).strip()
                delivery = str(row[delivery_c]).strip() if pd.notna(row[delivery_c]) else "自配送"

                if sku not in sku_data:
                    sku_data[sku] = {
                        "asin": asin,
                        "name": name,
                        "delivery": delivery,
                        "sites": [],
                    }
                if site not in sku_data[sku]["sites"]:
                    sku_data[sku]["sites"].append(site)

            # 批量创建商品节点
            products_list = [
                {
                    "sku": sku,
                    "asin": info["asin"],
                    "name": info["name"],
                    "delivery": info["delivery"],
                    "sites": info["sites"],
                }
                for sku, info in sku_data.items()
            ]

            session.run(
                """
                UNWIND $products AS p
                MERGE (prod:Product {sku: p.sku})
                SET prod.asin = p.asin,
                    prod.name = p.name,
                    prod.delivery_method = p.delivery,
                    prod.sites = p.sites
                """,
                products=products_list,
            )

            # 创建 LISTS_PRODUCT 关系
            relation_count = 0
            for sku, info in sku_data.items():
                result = session.run(
                    """
                    MATCH (prod:Product {sku: $sku})
                    UNWIND $sites AS site_name
                    MATCH (s:Site {name: site_name})
                    MERGE (s)-[:LISTS_PRODUCT]->(prod)
                    RETURN count(*) AS cnt
                    """,
                    sku=sku,
                    sites=info["sites"],
                )
                for record in result:
                    relation_count += record["cnt"]

            stats = {
                "sites": len(sites),
                "products": len(sku_data),
                "relations": relation_count,
            }
            return stats

    finally:
        driver.close()


# ── 向量数据库入库（LightRAG / Qdrant）──

async def build_vector_index(df: pd.DataFrame) -> dict:
    """通过 LightRAG 将产品文本索引到 Qdrant 向量库。

    每个产品生成一段描述文本，包含 SKU、品名、站点、配送方式等信息。
    LightRAG 负责 embedding + Qdrant 入向量 + Neo4j 实体抽取。
    """
    idx = get_indexer()
    await idx._ensure_initialized_async()

    cols = list(df.columns)
    site_c, sku_c, asin_c, name_c, delivery_c = cols[0], cols[1], cols[2], cols[3], cols[4]

    # 按 SKU 聚合站点信息
    sku_info: dict[str, list[tuple[str, str, str, str]]] = {}
    for _, row in df.iterrows():
        sku = str(row[sku_c]).strip()
        site = str(row[site_c]).strip()
        asin = str(row[asin_c]).strip()
        name = str(row[name_c]).strip()
        delivery = str(row[delivery_c]).strip() if pd.notna(row[delivery_c]) else "自配送"
        sku_info.setdefault(sku, []).append((site, asin, name, delivery))

    # 批量索引（限并发数，避免 DashScope 限流）
    sem = asyncio.Semaphore(3)
    success = 0
    fail = 0
    total = len(sku_info)

    async def _index_one(sku: str, entries: list[tuple[str, str, str, str]]):
        nonlocal success, fail
        async with sem:
            # 构建描述文本
            parts = [f"SKU: {sku}"]
            for site, asin, name, delivery in entries:
                parts.append(
                    f"站点{site} | ASIN: {asin} | 品名: {name} | 配送方式: {delivery}"
                )
            text = "\n".join(parts)

            try:
                await idx._rag.ainsert(text, file_paths=[f"qiaodoudou:{sku}"])
                success += 1
            except Exception as exc:
                logger.warning(f"向量索引失败 [{sku}]: {exc}")
                fail += 1

            if (success + fail) % 50 == 0:
                logger.info(f"向量索引进度: {success + fail}/{total} (成功 {success}, 失败 {fail})")

    tasks = [_index_one(sku, entries) for sku, entries in sku_info.items()]
    await asyncio.gather(*tasks)

    return {"success": success, "fail": fail, "total": total}


# ── 主流程 ──

async def main():
    # 读取 Excel
    logger.info("读取 Excel: %s", EXCEL_PATH)
    df = pd.read_excel(EXCEL_PATH)
    logger.info("共 %d 行, %d 列, %d 独立 SKU", len(df), len(df.columns), df.iloc[:, 1].nunique())

    # 1. 导入知识图谱（Neo4j）
    logger.info("=" * 50)
    logger.info("Step 1: 构建知识图谱 (Neo4j)")
    kg_stats = await build_knowledge_graph(df)
    logger.info(
        "知识图谱完成: %d 站点, %d 商品, %d 条关系",
        kg_stats["sites"], kg_stats["products"], kg_stats["relations"],
    )

    # 2. 导入向量数据库（LightRAG → Qdrant）
    logger.info("=" * 50)
    logger.info("Step 2: 构建向量索引 (LightRAG → Qdrant)")
    logger.info("   这将为每个产品的描述文本生成 Embedding 并存入 Qdrant")
    logger.info("   预计耗时较长，请耐心等待...")
    vec_stats = await build_vector_index(df)
    logger.info(
        "向量索引完成: %d/%d 成功, %d 失败",
        vec_stats["success"], vec_stats["total"], vec_stats["fail"],
    )

    # 验证
    logger.info("=" * 50)
    logger.info("验证导入结果...")
    await verify()


async def verify():
    """验证数据是否正确写入。"""

    # Neo4j 验证
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    try:
        with driver.session() as session:
            # 公司节点
            r = session.run("MATCH (c:Company {name: $name}) RETURN c.name", name=COMPANY_NAME)
            company = r.single()
            logger.info("公司节点: %s", company[0] if company else "未找到！")

            # 站点数量
            r = session.run("MATCH (s:Site) RETURN count(s) AS cnt")
            site_cnt = r.single()["cnt"]
            logger.info("站点数量: %d", site_cnt)

            # 商品数量
            r = session.run("MATCH (p:Product) RETURN count(p) AS cnt")
            prod_cnt = r.single()["cnt"]
            logger.info("商品数量: %d", prod_cnt)

            # LISTS_PRODUCT 关系
            r = session.run("MATCH ()-[r:LISTS_PRODUCT]->() RETURN count(r) AS cnt")
            rel_cnt = r.single()["cnt"]
            logger.info("LISTS_PRODUCT 关系: %d", rel_cnt)

            # 示例：查看 US 站点下的前 3 个商品
            r = session.run(
                """
                MATCH (s:Site {name: 'US'})-[:LISTS_PRODUCT]->(p:Product)
                RETURN s.name AS site, p.sku AS sku, p.name AS name, p.asin AS asin
                LIMIT 3
                """
            )
            logger.info("US 站点示例商品:")
            for record in r:
                logger.info("  [%s] %s | %s | ASIN=%s",
                            record["site"], record["sku"], record["name"][:60], record["asin"])

            # 跨站点商品示例
            r = session.run(
                """
                MATCH (p:Product)
                WHERE size(p.sites) > 1
                RETURN p.sku AS sku, p.name AS name, p.sites AS sites
                LIMIT 3
                """
            )
            logger.info("跨站点商品示例:")
            for record in r:
                logger.info("  %s: 站点=%s", record["sku"], record["sites"])

    finally:
        driver.close()

    # LightRAG 验证
    idx = get_indexer()
    await idx._ensure_initialized_async()

    # 查 doc_status 确认入库数量
    from lightrag.base import DocStatus
    docs = await idx._rag.doc_status.get_docs_by_statuses([DocStatus.PROCESSED])
    qd_count = len(docs)
    logger.info("LightRAG 已索引文档数: %d", qd_count)

    # 尝试向量搜索
    result = await idx._search_async("毛毛虫", 3)
    has_result = "[no-context]" not in result
    logger.info("向量搜索'毛毛虫': %s", "有结果" if has_result else "无结果")


if __name__ == "__main__":
    asyncio.run(main())
