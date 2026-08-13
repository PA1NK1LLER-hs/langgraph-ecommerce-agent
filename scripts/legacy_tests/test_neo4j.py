"""测试查询 Neo4j 中 LightRAG 存储的数据。"""

import sys
sys.path.insert(0, "src")

from dotenv import load_dotenv
load_dotenv()

from neo4j import GraphDatabase
from config import NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD


def test_neo4j_search():
    """测试 Neo4j 中的数据查询。"""
    print("=" * 80)
    print("🔍 Neo4j 数据查询测试")
    print("=" * 80)

    driver = GraphDatabase.driver(
        NEO4J_URI,
        auth=(NEO4J_USERNAME, NEO4J_PASSWORD)
    )

    try:
        with driver.session() as session:
            # 1. 查看所有节点标签
            print("\n【1】数据库中的节点标签")
            print("-" * 80)
            result = session.run("CALL db.labels() YIELD label RETURN label")
            labels = [record['label'] for record in result]
            if labels:
                for label in labels:
                    result = session.run(f"MATCH (n:{label}) RETURN count(n) AS count")
                    count = result.single()['count']
                    print(f"  {label}: {count} 个节点")
            else:
                print("  没有找到任何节点标签")

            # 2. 查看所有属性键
            print("\n【2】数据库中的属性键")
            print("-" * 80)
            result = session.run("CALL db.propertyKeys() YIELD propertyKey RETURN propertyKey")
            properties = [record['propertyKey'] for record in result]
            for prop in properties:
                print(f"  - {prop}")

            # 3. 查看示例节点
            print("\n【3】示例节点（前5个）")
            print("-" * 80)
            result = session.run("""
                MATCH (n)
                RETURN labels(n) AS 标签, keys(n) AS 属性名, n
                LIMIT 5
            """)

            for i, record in enumerate(result, 1):
                print(f"\n节点 #{i}:")
                print(f"  标签: {record['标签']}")
                print(f"  属性名: {record['属性名']}")

                # 打印属性值
                node_data = record['n']
                for prop in record['属性名'][:5]:  # 只显示前5个
                    value = node_data.get(prop)
                    if value:
                        value_str = str(value)[:100]
                        print(f"    {prop}: {value_str}")

            # 4. 查询特定来源的数据
            print("\n【4】查询特定来源的数据")
            print("-" * 80)

            # 查看所有不同的 file_path
            result = session.run("""
                MATCH (n)
                WHERE n.file_path IS NOT NULL
                RETURN DISTINCT n.file_path AS 来源, count(*) AS 数量
                ORDER BY 数量 DESC
            """)

            sources = list(result)
            if sources:
                print("  数据来源:")
                for record in sources:
                    print(f"    - {record['来源']}: {record['数量']} 个节点")

                # 查询最新的数据源
                if sources:
                    latest_source = sources[0]['来源']
                    print(f"\n  查询最新来源 '{latest_source}' 的数据:")

                    result = session.run("""
                        MATCH (n)
                        WHERE n.file_path = $source
                        RETURN n
                        LIMIT 10
                    """, source=latest_source)

                    nodes = list(result)
                    if nodes:
                        for i, record in enumerate(nodes, 1):
                            node_data = record['n']
                            print(f"\n  节点 #{i}:")
                            # 显示主要属性
                            for prop in ['entity_id', 'entity_name', 'name', 'description', 'entity_type']:
                                if prop in node_data:
                                    value = node_data[prop]
                                    print(f"    {prop}: {str(value)[:80]}")
                    else:
                        print("  没有找到节点")
            else:
                print("  没有找到带 file_path 的节点")

            # 5. 搜索特定关键词
            print("\n【5】搜索特定关键词")
            print("-" * 80)

            keywords = ["郑钰莹", "美国站", "毛绒毛毛虫"]
            for keyword in keywords:
                print(f"\n  搜索关键词: '{keyword}'")
                result = session.run("""
                    MATCH (n)
                    WHERE ANY(key IN keys(n) 
                              WHERE toString(n[key]) CONTAINS $keyword)
                    RETURN n
                    LIMIT 3
                """, keyword=keyword)

                nodes = list(result)
                if nodes:
                    for i, record in enumerate(nodes, 1):
                        node_data = record['n']
                        print(f"    找到节点 #{i}:")
                        for prop in ['entity_id', 'entity_name', 'name', 'description']:
                            if prop in node_data:
                                value = str(node_data[prop])[:80]
                                print(f"      {prop}: {value}")
                else:
                    print(f"    未找到包含 '{keyword}' 的节点")

            # 6. 查看关系
            print("\n【6】查看关系（前5条）")
            print("-" * 80)
            result = session.run("""
                MATCH (a)-[r]->(b)
                RETURN type(r) AS 关系类型, 
                       a.entity_id AS 源实体, 
                       b.entity_id AS 目标实体,
                       keys(r) AS 关系属性
                LIMIT 5
            """)

            relations = list(result)
            if relations:
                for i, record in enumerate(relations, 1):
                    print(f"\n  关系 #{i}:")
                    print(f"    类型: {record['关系类型']}")
                    print(f"    源实体: {record['源实体']}")
                    print(f"    目标实体: {record['目标实体']}")
                    print(f"    关系属性: {record['关系属性']}")
            else:
                print("  没有找到关系")

            # 7. 统计信息
            print("\n【7】数据统计")
            print("-" * 80)

            # 节点总数
            result = session.run("MATCH (n) RETURN count(n) AS total")
            total_nodes = result.single()['total']
            print(f"  总节点数: {total_nodes}")

            # 关系总数
            result = session.run("MATCH ()-[r]->() RETURN count(r) AS total")
            total_rels = result.single()['total']
            print(f"  总关系数: {total_rels}")

            # 按标签统计
            print("\n  按节点类型统计:")
            result = session.run("""
                MATCH (n)
                RETURN labels(n)[0] AS 类型, count(*) AS 数量
                ORDER BY 数量 DESC
            """)
            for record in result:
                print(f"    {record['类型']}: {record['数量']}")

    except Exception as e:
        print(f"\n❌ 查询失败: {e}")
        import traceback
        traceback.print_exc()
    finally:
        driver.close()

    print("\n" + "=" * 80)
    print("✅ 查询完成！")
    print("=" * 80)
    print("\n💡 提示:")
    print("  1. 以上查询可以复制到 Neo4j Browser (http://localhost:7474) 执行")
    print("  2. 点击返回的节点可以在图形界面中查看详细信息")
    print("  3. 使用 Graph 视图可以直观地看到节点之间的关系")


if __name__ == "__main__":
    test_neo4j_search()
