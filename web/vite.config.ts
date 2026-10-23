import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

// Same-origin development proxy. The browser talks to this dev server only; /mcp, /healthz and /operator
// (personal voice tagging, cookie-authenticated) are forwarded,
// unchanged (Authorization header included), to the CloudGuard HTTP MCP server. The proxy adds no credentials,
// so the backend still authenticates and authorizes every request: it is not an unauthenticated proxy.
// No CORS is needed on the backend, and its DNS-rebinding protection stays on: the target's Host is rewritten to
// the backend's own address (changeOrigin), and the browser's Origin (http://127.0.0.1:5173) is a loopback
// origin the backend already allows.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "CLOUDGUARD_");
  const target = env.CLOUDGUARD_MCP_TARGET || "http://127.0.0.1:8080";
  const proxy = { target, changeOrigin: true, secure: true };
  return {
    plugins: [react()],
    server: {
      host: "127.0.0.1",
      port: 5173,
      strictPort: true,
      proxy: { "/mcp": proxy, "/healthz": proxy, "/operator": proxy },
    },
    preview: { host: "127.0.0.1", port: 4173, strictPort: true, proxy: { "/mcp": proxy, "/healthz": proxy, "/operator": proxy } },
  };
});
