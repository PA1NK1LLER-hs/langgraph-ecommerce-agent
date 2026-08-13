"""查看 LightRAG 在 Qdrant 中 chunks、entities、relationships 的联系。"""

import sys

sys.path.insert(0, "src")

from dotenv import load_dotenv

load_dotenv()

from qdrant_client import QdrantClient
from config import QDRANT_URL


def show_connections():
    """展示三个集合之间的联系。"""
    client = QdrantClient(url=QDRANT_URL)

    print("=" * 80)
    print("LightRAG Qdrant 数据结构联系分析")
    print("=" * 80)

    # 1. 查看一个具体的chunk
    print("\n【1】查看文本分块 (chunks)")
    print("-" * 80)
    chunks_points = client.scroll(
        collection_name="lightrag_vdb_chunks_text_embedding_v3_1024d",
        limit=3,
        with_payload=True,
        with_vectors=False
    )[0]

    for i, point in enumerate(chunks_points, 1):
        print(f"\nChunk #{i}:")
        print(f"  ID: {point.id}")
        content = point.payload.get('content', '')
        print(f"  内容: {content[:150]}...")
        print(f"  file_path: {point.payload.get('file_path')}")
        print(f"  chunk_order_index: {point.payload.get('chunk_order_index')}")

    # 2. 查看实体及其与chunk的关联
    print("\n\n【2】查看实体 (entities) - 注意 source_id 关联")
    print("-" * 80)
    entities_points = client.scroll(
        collection_name="lightrag_vdb_entities_text_embedding_v3_1024d",
        limit=5,
        with_payload=True,
        with_vectors=False
    )[0]

    for i, point in enumerate(entities_points, 1):
        print(f"\nEntity #{i}:")
        print(f"  ID: {point.id}")
        print(f"  实体名称: {point.payload.get('entity_name')}")
        print(f"  实体类型: {point.payload.get('entity_type')}")
        print(f"  描述: {point.payload.get('description', '')[:100]}")
        print(f"  source_id: {point.payload.get('source_id')}  ← 关联到chunk")
        print(f"  file_path: {point.payload.get('file_path')}")

    # 3. 查看关系及其与chunk的关联
    print("\n\n【3】查看关系 (relationships) - 注意 source_id 关联")
    print("-" * 80)
    relations_points = client.scroll(
        collection_name="lightrag_vdb_relationships_text_embedding_v3_1024d",
        limit=5,
        with_payload=True,
        with_vectors=False
    )[0]

    for i, point in enumerate(relations_points, 1):
        print(f"\nRelation #{i}:")
        print(f"  ID: {point.id}")
        print(f"  源实体: {point.payload.get('src_id')}")
        print(f"  目标实体: {point.payload.get('tgt_id')}")
        print(f"  关系类型: {point.payload.get('relationship_type')}")
        print(f"  描述: {point.payload.get('description', '')[:100]}")
        print(f"  source_id: {point.payload.get('source_id')}  ← 关联到chunk")
        print(f"  file_path: {point.payload.get('file_path')}")

    # 4. 总结联系
    print("\n\n" + "=" * 80)
    print("📊 数据结构联系总结")
    print("=" * 80)
    print("""
┌──────────────┐
│  原始文档     │
└──────┬───────┘
       │ 分块
       ↓
┌──────────────┐
│   Chunks     │ ← 存储原始文本片段
│  (文本分块)   │    ID: chunk-xxx
└──────┬───────┘    file_path: xxx
       │ LLM抽取
       ├──────────────────────┐
       ↓                      ↓
┌──────────────┐    ┌──────────────────┐
│  Entities    │    │  Relationships   │
│  (实体)      │    │  (关系)          │
└──────────────┘    └──────────────────┘
       ↑                      ↑
       │ source_id            │ source_id
       │ 指向 chunk ID        │ 指向 chunk ID
       │                      │
       └──────────────────────┘
              都来自同一个 chunk!

关键联系字段:
  1. source_id: entity/relationship → chunk (追溯来源)
  2. file_path: 所有集合都有 (按来源过滤)
  3. entity_name: entities ↔ Neo4j节点 (图结构对应)
  4. src_id/tgt_id: relationships ↔ Neo4j边 (图结构对应)
    """)


if __name__ == "__main__":
    try:
        show_connections()
    except Exception as e:
        print(f"❌ 错误: {e}")
        import traceback

        traceback.print_exc()
