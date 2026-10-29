"""Verify the MCP wrapper: tools registered, and the proxy reaches the REST service."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_service import mcp_server


async def main() -> None:
    # 1. what tools does the server advertise?
    names = []
    manager = getattr(mcp_server.mcp, "_tool_manager", None)
    if manager is not None:
        for attr in ("list_tools", "_tools"):
            candidate = getattr(manager, attr, None)
            if candidate is None:
                continue
            if attr == "list_tools":
                try:
                    tools = candidate()
                    if asyncio.iscoroutine(tools):
                        tools = await tools
                    names = [t.name for t in tools]
                    break
                except Exception:  # noqa: BLE001
                    continue
            if isinstance(candidate, dict):
                names = sorted(candidate)
                break
    print(f"MCP tools registered ({len(names)}):")
    for name in names:
        print(f"  - {name}")

    # 2. does the proxy reach the REST service?
    print("\nproxy check:")
    for label, method, path, kwargs in (
        ("price AAPL", "GET", "/price/AAPL", {"params": {"venue": "US"}}),
        ("atr AAPL", "POST", "/atr", {"json": {"symbols": ["AAPL"], "period": 14}}),
        ("bars AAPL", "POST", "/bars", {"json": {"symbols": ["AAPL"], "sessions": 5}}),
        ("stats", "GET", "/stats", {}),
    ):
        result = await mcp_server._request(method, path, **kwargs)
        if "error" in result:
            print(f"  {label:<12} ERROR {result.get('error')}: {result.get('detail', '')[:90]}")
            continue
        if label == "stats":
            print(f"  {label:<12} ok, database={result.get('database')}")
        elif label == "price AAPL":
            print(f"  {label:<12} ok, provider={result.get('provider')} price={result.get('price')}")
        elif label == "atr AAPL":
            reading = (result.get("atr") or {}).get("AAPL") or {}
            print(f"  {label:<12} ok, value={reading.get('value')} method={reading.get('method')}")
        else:
            entry = (result.get("bars") or {}).get("AAPL") or {}
            print(f"  {label:<12} ok, sessions={entry.get('sessions')} "
                  f"as_of={entry.get('as_of_session')}")


asyncio.run(main())
