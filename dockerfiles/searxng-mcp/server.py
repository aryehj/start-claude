from mcp.server.mcpserver import MCPServer
import httpx, os

mcp = MCPServer("searxng")
URL = os.environ["SEARXNG_URL"]

@mcp.tool()
async def websearch(query: str, categories: str = "general",
                    time_range: str | None = None, language: str = "en") -> list[dict]:
    """Search the web via a local SearXNG instance. Returns top results as {title, url, content}."""
    params = {"q": query, "format": "json", "categories": categories, "language": language}
    if time_range:
        params["time_range"] = time_range
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get(f"{URL}/search", params=params)
        r.raise_for_status()
        return [{"title": x["title"], "url": x["url"], "content": x.get("content", "")}
                for x in r.json().get("results", [])[:10]]

if __name__ == "__main__":
    mcp.run()
