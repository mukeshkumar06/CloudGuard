// Loading one read-only tool's answer for a dashboard panel, through the app's own MCP pipeline (run()), so the
// answer cache, the in-flight de-duplication and the Results/Activity history all apply. Never invents data.
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { useCallback, useEffect, useRef, useState } from "react";

import type { LoadStatus } from "./kit";

export interface RunOutcome { result?: CallToolResult; error?: { message: string } }
export type Run = (name: string, args: Record<string, unknown>, mode?: "use" | "refresh" | "bypass") => Promise<RunOutcome>;

export interface ToolData<T> { status: LoadStatus; data?: T; error?: string; load: (refresh?: boolean) => Promise<void> }

/** `auto`: load when the panel first appears (fixture mode; live cost panels wait for a click, as each Cost
 *  Explorer request is billed). A changed argument set loads again only if it was loaded before or `auto`. */
export function useToolData<T>(run: Run, available: string[], tool: string, args: Record<string, unknown>,
                               auto: boolean): ToolData<T> {
  const key = `${tool}:${JSON.stringify(args)}`;
  const [state, setState] = useState<{ key: string; status: LoadStatus; data?: T; error?: string }>(
    { key, status: "idle" });
  const seq = useRef(0);
  const has = available.includes(tool);
  const load = useCallback(async (refresh = false) => {
    if (!available.includes(tool)) return;
    const n = ++seq.current;
    setState((s) => ({ ...s, key, status: "loading" }));
    const out = await run(tool, args, refresh ? "refresh" : "use");
    if (n !== seq.current) return;
    if (out.error) setState({ key, status: "error", error: out.error.message });
    else if (out.result?.isError) {
      const text = (out.result.content?.[0] as { text?: string } | undefined)?.text;
      setState({ key, status: "tool-error", error: text?.replace(/^Error executing tool \w+: /, "") });
    } else setState({ key, status: "ok", data: out.result?.structuredContent as T });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run, key, has]);
  useEffect(() => {
    if (has && auto && (state.key !== key || state.status === "idle")) void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, has, auto]);
  const status: LoadStatus = !has ? "unavailable" : state.key === key ? state.status : "idle";
  return { status, data: state.key === key ? state.data : undefined, error: state.error, load };
}
