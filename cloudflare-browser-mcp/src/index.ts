import { env } from "cloudflare:workers";
import { createMcpAgent } from "@cloudflare/playwright-mcp";

export const PlaywrightMCP = createMcpAgent((env as any).BROWSER);

export default {
  async fetch(request: Request, env: any, ctx: any) {
    const { pathname } = new URL(request.url);

    switch (pathname) {
      case "/sse":
      case "/sse/message":
        return PlaywrightMCP.serveSSE("/sse").fetch(request, env, ctx);
      case "/mcp":
        return PlaywrightMCP.serve("/mcp").fetch(request, env, ctx);
      default:
        return new Response("Cloudflare Playwright MCP Server is Running! Use /sse or /mcp endpoint.", {
          status: 200,
          headers: { "Content-Type": "text/plain; charset=utf-8" },
        });
    }
  },
};
