"""Agent interactive demo — natural language → autonomous browser operation."""
import sys, os, uuid, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, "src")
from dotenv import load_dotenv
load_dotenv()

from skills import register_skill, discover_and_register
discover_and_register(register_skill)
from agent.graph import build_agent
from langchain_core.messages import HumanMessage

agent = build_agent()

def run(msg):
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    step = 0
    for chunk in agent.stream(
        {"messages": [HumanMessage(content=msg)]},
        config=config,
        stream_mode="updates",
    ):
        for node_name, node_output in chunk.items():
            for m in node_output.get("messages", []):
                if m.type == "ai":
                    if hasattr(m, "tool_calls") and m.tool_calls:
                        for tc in m.tool_calls:
                            step += 1
                            name = tc["name"]
                            args = str(tc.get("args", {}))[:200]
                            print(f"  [{step}] >> {name}({args})")
                    elif m.content:
                        text = m.content[:300].encode("ascii", errors="replace").decode("ascii")
                        model = (m.additional_kwargs or {}).get("_model_used", "")
                        route_reason = (m.additional_kwargs or {}).get("_route_reason", "")
                        if text.strip():
                            print(f"  [AI/{model}] {text}")
                            if route_reason:
                                print(f"       [route] {route_reason}")
                elif m.type == "tool":
                    res = str(m.content)[:300].encode("ascii", errors="replace").decode("ascii")
                    status = "ERR" if "error" in res.lower() else "OK"
                    print(f"       [{status}] {getattr(m, 'name', '?')}")


print("=" * 60)
print("User: 查询昨日广告花费 — 美国站关键词[毛绒毛毛虫]")
print("=" * 60)
run("查询巧逗豆-US店铺昨日广告花费，操作人何山，美国站，关键词只有毛绒毛毛虫这一个")

