"""LightRAG knowledge base admin - browse, search, delete indexed documents.

Usage:
  kb_admin.py list              # List all sources
  kb_admin.py search <keyword>  # Search knowledge base
  kb_admin.py delete <source>   # Delete a source
  kb_admin.py stats             # Show statistics
"""

import sys
sys.path.insert(0, "src")
from rag.indexer import get_indexer


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]
    idx = get_indexer()

    if cmd == "list":
        sources = idx.list_sources()
        if not sources:
            print("(知识库为空)")
            return
        print(f"\n共 {len(sources)} 个来源，{idx.count()} 个分块\n")
        for src in sources:
            print(f"  [{src['chunks']:>4} chunks]  {src['source']}")

    elif cmd == "search":
        if len(sys.argv) < 3:
            print("用法: kb_admin.py search <关键词>")
            return
        query = sys.argv[2]
        r = idx.search(query, n_results=10, use_rerank=False)
        print(f"\n搜索「{query}」— 找到 {r['count']} 条:\n")
        for i, item in enumerate(r["results"], 1):
            # B5 修复: 兼容新旧两种返回格式
            score = item.get('score', 0.0)
            content = item.get('content', '')[:120]
            source = item.get('source', 'unknown')
            print(f"  [{i}] [{score}]  {content}")
            print(f"      来源: {source}")

    elif cmd == "delete":
        if len(sys.argv) < 3:
            print("用法: kb_admin.py delete <来源名>")
            return
        source = sys.argv[2]
        n = idx.delete_source(source)
        print(f"已删除来源「{source}」的 {n} 个分块")

    elif cmd == "stats":
        print(f"\n总分块数: {idx.count()}")
        print(f"总来源数: {len(idx.list_sources())}")

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
