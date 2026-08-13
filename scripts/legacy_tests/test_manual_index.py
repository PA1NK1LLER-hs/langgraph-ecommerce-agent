"""手动向 LightRAG 知识库写入测试数据，自动同步写入 Qdrant + Neo4j。

直接在 PyCharm 中运行即可。
如需清空 Neo4j，修改 clear_first = True
"""

import sys
sys.path.insert(0, "src")

from dotenv import load_dotenv
load_dotenv()

from rag import index_knowledge, async_index_knowledge, async_search_knowledge, search_knowledge
import asyncio
import time


def clear_neo4j():
    """清空 Neo4j 数据库中的所有节点和关系，同时清理 LightRAG 的 doc_status 持久化文件。"""
    print("\n" + "=" * 80)
    print("🗑️  清空 Neo4j 数据库")
    print("=" * 80)

    from neo4j import GraphDatabase
    from pathlib import Path
    from config import NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, LIGHTRAG_WORKING_DIR

    # 0. 清理 LightRAG 持久化的 doc_status，避免上次运行的残留状态被恢复
    doc_status_file = Path(LIGHTRAG_WORKING_DIR) / "kv_store_doc_status.json"
    if doc_status_file.exists():
        doc_status_file.unlink()
        print("  已清理 doc_status 持久化文件")

    driver = GraphDatabase.driver(
        NEO4J_URI,
        auth=(NEO4J_USERNAME, NEO4J_PASSWORD)
    )

    try:
        with driver.session() as session:
            # 1. 统计当前数据量
            print("\n【1】当前数据统计")
            print("-" * 80)

            result = session.run("MATCH (n) RETURN count(n) AS total")
            total_nodes = result.single()['total']
            print(f"  节点总数: {total_nodes}")

            result = session.run("MATCH ()-[r]->() RETURN count(r) AS total")
            total_rels = result.single()['total']
            print(f"  关系总数: {total_rels}")

            if total_nodes == 0:
                print("\n  ✅ 数据库已经是空的，无需清空。")
                return True

            # 2. 执行删除
            print(f"\n【2】开始删除 {total_nodes} 个节点和 {total_rels} 条关系...")
            print("-" * 80)

            result = session.run("MATCH (n) DETACH DELETE n")
            summary = result.consume()

            deleted_nodes = summary.counters.nodes_deleted
            deleted_rels = summary.counters.relationships_deleted

            print(f"  ✅ 成功删除 {deleted_nodes} 个节点")
            print(f"  ✅ 成功删除 {deleted_rels} 条关系")

            # 3. 验证删除结果
            print("\n【3】验证删除结果")
            print("-" * 80)

            result = session.run("MATCH (n) RETURN count(n) AS total")
            remaining_nodes = result.single()['total']
            print(f"  剩余节点: {remaining_nodes}")

            result = session.run("MATCH ()-[r]->() RETURN count(r) AS total")
            remaining_rels = result.single()['total']
            print(f"  剩余关系: {remaining_rels}")

            if remaining_nodes == 0 and remaining_rels == 0:
                print("\n✅ Neo4j 数据库已成功清空！")
                return True
            else:
                print(f"\n️  仍有数据残留")
                return False

    except Exception as e:
        print(f"\n❌ 清空失败: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        driver.close()

    print("=" * 80 + "\n")


def operator_data_to_text(operator: str, sites: dict) -> str:
    """将单个操作人的数据转为自然语言。"""
    lines = []
    for site, keywords in sites.items():
        if keywords and keywords != ['/']:
            kws = "、".join(keywords)
            lines.append(f"{operator} 在{site}负责以下产品：{kws}。")
    return "\n".join(lines)


def main():
    import traceback

    # ==================== 配置区 ====================
    # 设置为 True 会先清空 Neo4j 再插入
    # 设置为 False 则增量插入（保留现有数据）
    clear_first = True
    # ================================================

    # 如果需要清空，先执行清空
    if clear_first:
        if not clear_neo4j():
            print("\n❌ Neo4j清空失败，程序终止")
            return
        print()

    print("=" * 80)
    print("开始插入数据到 LightRAG")
    print("=" * 80)

    try:
        # 操作人数据
        campaign_json = {
            '郑钰莹': {'美国站': ['儿童类目毛绒毛毛虫', '儿童类目呼吸革毛毛虫', '灯芯绒贵妃位', '儿童类目甜甜圈']},
            '林梦娇': {'加拿大站': ['水滴款', '小兔毛扶手', '新月亮椅'],
                       '德国站': ['水滴款'],
                       '澳洲站': ['小兔毛扶手'],
                       '美国站': ['水滴款', '小兔毛扶手', '新月亮椅', '圆弧贵妃', '云朵沙发', '羊羔绒贵妃'],
                       '英国站': ['水滴款']},
            '禹世豪': {'加拿大站': ['荷兰绒豆袋椅', '泡泡绒豆袋椅', '素色大方格豆袋椅', '毛绒毛毛虫'],
                       '德国站': ['毛绒毛毛虫'],
                       '澳洲站': ['荷兰绒豆袋椅', '毛绒毛毛虫'],
                       '美国本土店铺': ['Velvet Bean Bag Chair', 'Modular Sectional Sofa'],
                       '美国站': ['竖纹横纹贵妃沙发', '荷兰绒豆袋椅', '泡泡绒豆袋椅', '毛绒毛毛虫',
                                  '素色大方格豆袋椅', '泡泡绒套垫沙发', '超柔粗条绒三用款',
                                  '贝贝兔毛豆袋椅', '帆船沙发', '6FT各面料豆袋椅集合', '仿羽绒沙发'],
                       '英国站': ['毛绒毛毛虫']},
            '陈丽': {'加拿大站': ['PU毛毛虫', '背印豆袋椅', '粗条绒豆袋椅', '水波纹豆袋椅',
                                  '菱形豆袋椅', '刷花豆袋椅', '梯形沙发'],
                     '德国站': ['/'],
                     '澳洲站': ['刷花豆袋椅', '梯形沙发'],
                     '美国站': ['PU毛毛虫', '背印兔毛豆袋椅', '粗条绒豆袋椅', '水波纹豆袋椅',
                                '菱形豆袋椅', '刷花豆袋椅', '刷花三用', '兔毛三用', '梯形沙发',
                                '动物地垫', 'PU少年儿童'],
                     '英国站': ['/']},
            '董星禹': {'加拿大站': ['粗条绒豆袋船', '荷兰绒豆袋船', '兔毛豆袋船', '刷花豆袋船',
                                    '泡泡绒豆袋船', '水波纹豆袋船', '素色大方格豆袋船',
                                    '连体', '高背', '托卡月亮椅'],
                       '德国站': ['豆袋船', '连体', '高背'],
                       '澳洲站': ['豆袋船', '连体'],
                       '美国站': ['粗条绒豆袋船', '荷兰绒豆袋船', '兔毛豆袋船', '刷花豆袋船',
                                  '泡泡绒豆袋船', '水波纹豆袋船', '素色大方格豆袋船',
                                  '连体', '刷花躺椅', '组合沙发', '高背', '超柔冬瓜豆袋椅', '雪尼尔钉扣'],
                       '英国站': ['豆袋船', '连体', '高背']},
            '何蕊': {'加拿大站': ['小兔毛豆袋椅', '兔毛甜甜圈', '泡泡绒甜甜圈', '折叠', '卡车沙发'],
                     '德国站': ['兔毛甜甜圈', '泡泡绒甜甜圈'],
                     '澳洲站': ['甜甜圈', '折叠'],
                     '美国站': ['小兔毛豆袋椅', '泡泡绒钉扣', '兔毛甜甜圈', '泡泡绒甜甜圈',
                                '折叠', '粗条绒冬瓜豆袋椅'],
                     '英国站': ['兔毛甜甜圈', '泡泡绒甜甜圈']}}

        # 插入和检索都在同一个事件循环中完成，避免 asyncio.run() 反复创建/销毁循环
        # 导致 Neo4j 连接池锁损坏和 PriorityQueue 事件循环绑定错误
        asyncio.run(_run_inserts_and_search(campaign_json))

    except Exception as e:
        print(f"\n 发生异常:")
        print(f"错误类型: {type(e).__name__}")
        print(f"错误信息: {e}")
        print("\n详细堆栈:")
        traceback.print_exc()


async def _run_inserts_and_search(campaign_json: dict):
    """在同一个事件循环中完成插入和检索验证。"""
    print(f"\n共有 {len(campaign_json)} 个操作人需要插入\n")

    success_count = 0
    fail_count = 0

    for operator, sites in campaign_json.items():
        text = operator_data_to_text(operator, sites)
        source = f"广告数据-{operator}"

        print(f"正在插入: {operator} ({len(sites)}个站点)")
        try:
            result = await async_index_knowledge(text=text, source=source)

            if result.get("status") == "success":
                print(f"  ✅ {operator} 插入成功")
                success_count += 1
            else:
                print(f"  ❌ {operator} 插入失败: {result.get('message')}")
                fail_count += 1
        except Exception as e:
            print(f"  ❌ {operator} 插入异常: {e}")
            fail_count += 1

        await asyncio.sleep(2)

    print(f"\n{'=' * 80}")
    print(f"插入完成: 成功 {success_count} 个, 失败 {fail_count} 个")
    print(f"{'=' * 80}\n")

    if success_count > 0:
        print("✅ 数据写入成功！")

    # 验证检索（在同一个事件循环中，避免 PriorityQueue 绑定错误）
    print("\n正在验证检索...")
    test_query = "郑钰莹"
    print(f"\n测试查询: {test_query}")
    search_result = await async_search_knowledge(test_query, top_k=3)

    if search_result.get("status") == "success":
        results = search_result.get("results", [])
        if results:
            print(f"\n✓ 找到 {len(results)} 条结果:")
            for i, item in enumerate(results, 1):
                content = item.get("content", "")[:200]
                score = item.get("score", 0)
                print(f"  [{i}] score={score:.3f}")
                print(f"      {content}...")
        else:
            print("  检索成功但未找到结果（可能数据还在索引中）")
    else:
        print(f"❌ 检索失败: {search_result.get('message')}")


if __name__ == "__main__":
    # main()
    # 验证检索
    print("\n正在验证检索...")
    test_query = "郑钰莹"
    print(f"\n测试查询: {test_query}")
    search_result = search_knowledge(test_query, top_k=3)
    print(f"检索结果: {search_result}")

    if search_result.get("status") == "success":
        results = search_result.get("results", [])
        if results:
            print(f"\n✓ 找到 {len(results)} 条结果:")
            for i, item in enumerate(results, 1):
                content = item.get("content", "")[:200]
                score = item.get("score", 0)
                print(f"  [{i}] score={score:.3f}")
                print(f"      {content}...")
        else:
            print("  检索成功但未找到结果（可能数据还在索引中）")
    else:
        print(f"❌ 检索失败: {search_result.get('message')}")
