"""Demo: connect to external Filesystem MCP server via npx."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


async def main():
    from mcp_wrapper.client import MCPToolImporter, StdioTransportConfig

    demo_dir = os.path.dirname(os.path.abspath(__file__))

    # Filesystem MCP server via npx (auto-downloads on first run)
    config = StdioTransportConfig(
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", demo_dir],
    )
    print("Connecting to Filesystem MCP Server...")
    print(f"  command: npx -y @modelcontextprotocol/server-filesystem {demo_dir}")
    print(f"  (first run may take a moment to download npm package)\n")

    async with MCPToolImporter(config) as importer:
        # 1. List tools
        tools = await importer.list_tools()
        print(f"Found {len(tools)} tools:\n")
        for t in tools:
            desc = t["description"][:120] if t["description"] else "(none)"
            print(f"  {t['name']}")
            print(f"    {desc}")
            # Show required params
            schema = t.get("inputSchema", {})
            props = schema.get("properties", {})
            required = schema.get("required", [])
            if props:
                for pname, pschema in props.items():
                    req_mark = " [required]" if pname in required else ""
                    ptype = pschema.get("type", "?")
                    pdesc = pschema.get("description", "")[:60] if pschema.get("description") else ""
                    print(f"      {pname}: {ptype}{req_mark}  {pdesc}")
            print()

        # 2. Import and call a tool: list_directory
        lc_tools = await importer.import_tools()
        print(f"Imported {len(lc_tools)} tools as LangChain format\n")

        # Find list_directory tool
        list_dir_tool = None
        read_file_tool = None
        for t in lc_tools:
            if t.name == "mcp_list_directory":
                list_dir_tool = t
            if t.name == "mcp_read_file":
                read_file_tool = t

        # 3. Call list_directory
        if list_dir_tool:
            print("=" * 50)
            print("Calling list_directory(path='/') ...")
            result = await list_dir_tool.coroutine(path="/")
            print(f"Status: {result['status']}")
            if result['status'] == 'success':
                for item in result['results']:
                    print(f"  {item[:120]}")
            print()

        # 4. Call read_file on the demo script itself
        if read_file_tool:
            print("=" * 50)
            print(f"Calling read_file(path='{__file__}') ...")
            result = await read_file_tool.coroutine(path=__file__)
            print(f"Status: {result['status']}")
            if result['status'] == 'success':
                for item in result['results']:
                    # File content - show first 300 chars
                    print(f"  {item[:300]}")
                    if len(item) > 300:
                        print(f"  ... ({len(item)} chars total)")

    print("\nDone - Filesystem MCP round-trip successful!")


if __name__ == "__main__":
    asyncio.run(main())
