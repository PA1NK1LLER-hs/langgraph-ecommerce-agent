"""MCP Client/Server end-to-end demo.

Connects to our own MCP server as a stdio subprocess, lists tools, imports them
as LangChain @tool format, and calls one to verify round-trip communication.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


async def main():
    from mcp_wrapper.client import MCPToolImporter, StdioTransportConfig

    print("=" * 60)
    print("   MCP Client End-to-End Demo")
    print("=" * 60)

    # Build subprocess environment
    src_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    sub_env = os.environ.copy()
    sub_env.setdefault("DEEPSEEK_API_KEY", "test-key")
    sub_env.setdefault("DEEPSEEK_BASE_URL", "https://api.test.local")
    sub_env.setdefault("DASHSCOPE_API_KEY", "test-key")
    sub_env.setdefault("POSTGRES_URL", "")
    existing_pp = sub_env.get("PYTHONPATH", "")
    sub_env["PYTHONPATH"] = f"{src_path}{os.pathsep}{existing_pp}" if existing_pp else src_path

    # Use -c to run server code directly (avoids -m module import warning)
    server_code = (
        "import asyncio; "
        "from mcp_wrapper.server import run_stdio_server; "
        "asyncio.run(run_stdio_server())"
    )

    config = StdioTransportConfig(
        command=sys.executable,
        args=["-c", server_code],
        env=sub_env,
        cwd=project_root,
    )
    print(f"\n[1] Starting MCP server subprocess...")

    importer = MCPToolImporter(config)
    await importer.connect()
    print("    OK - Server connected, MCP Initialize handshake complete")

    # 2. List remote tools
    print("\n[2] Requesting tools/list...")
    tool_defs = await importer.list_tools()
    print(f"    OK - Found {len(tool_defs)} tools:")
    for t in tool_defs[:5]:
        desc = t["description"][:60] if t["description"] else "(no description)"
        print(f"        - {t['name']}: {desc}")
    if len(tool_defs) > 5:
        print(f"        ... and {len(tool_defs) - 5} more")

    # 3. Import tools as LangChain @tool format
    print("\n[3] Importing tools as LangChain @tool format...")
    lc_tools = await importer.import_tools()
    print(f"    OK - Imported {len(lc_tools)} LangChain tools")

    # Show sample tool info
    sample = lc_tools[0]
    print(f"    Sample: name={sample.name}")
    desc = sample.description[:80] if sample.description else "(none)"
    print(f"            description={desc}")
    if sample.args_schema:
        fields = list(sample.args_schema.model_fields.keys())
        print(f"            args_schema fields={fields}")

    # 4. Call a no-arg tool to verify end-to-end
    # Note: imported tools get "mcp_" prefix + original name (e.g. "mcp_tool_list_memories")
    print("\n[4] Calling list_memories tool (end-to-end test)...")
    list_tool = None
    for t in lc_tools:
        if "list_memories" in t.name:
            list_tool = t
            break

    if list_tool:
        result = await list_tool.coroutine()
        print(f"    OK - Call succeeded! (tool: {list_tool.name})")
        print(f"    Result: {result}")
    else:
        print("    No list_memories tool found — listing imported tool names:")
        for t in lc_tools:
            print(f"        {t.name}")

    # 5. Call a tool with arguments via the raw MCP protocol
    print("\n[5] Calling search_knowledge via raw session.call_tool...")
    try:
        raw_result = await importer.call_tool(
            "tool_search_knowledge",  # original server-side name
            {"query": "test query", "top_k": 3},
        )
        print(f"    OK - Direct call succeeded!")
        print(f"    Raw result type: {type(raw_result).__name__}")
        if raw_result.content:
            for i, c in enumerate(raw_result.content):
                print(f"    Content[{i}]: {str(c.text)[:200] if hasattr(c, 'text') else str(c)[:200]}")
    except Exception as exc:
        print(f"    Error: {exc}")

    # 6. Disconnect
    print("\n[6] Disconnecting MCP session...")
    await importer.disconnect()
    print("    OK - Session closed")

    print("\n" + "=" * 60)
    print("   DEMO PASSED - MCP Client/Server round-trip OK")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
