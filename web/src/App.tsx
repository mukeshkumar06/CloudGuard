import type { CallToolResult, Tool } from "@modelcontextprotocol/sdk/types.js";
import { useEffect, useMemo, useRef, useState } from "react";

import { Conversation, type Msg } from "./components/Conversation";
import { OperatorCard } from "./components/OperatorCard";
import type { Execution } from "./components/ResultView";
import { useVoiceState } from "./components/VoicePanel";
import { AnswerCache, ageText, stableArgs, type CachedAnswer } from "./lib/cache";
import { answerRegion, answerResourceName, mapIntent } from "./lib/intent";
import { spokenAnswer } from "./lib/resources";
import { connect, serverMode, SimulatorError, type Connection } from "./lib/mcp";
import { browserVoiceDeps, defaultRecognitionLang, FOLLOW_UP_TEXT, PREVIEW_TEXT, VoiceController, type ExecuteResult } from "./lib/voice";
import { regionSupport, requiresConfirmation, withRefresh } from "./lib/tools";
import { isListing, regionAnswer, regionOnly, searchingText, withRegion } from "./lib/discovery";
import { regionChoices, regionQuestion } from "./lib/regions";
import { unchangedReason } from "./lib/voicewrite";
import { engageKillSwitch, fetchOperatorSession, logoutOperator, operatorApi, type OperatorSession } from "./lib/operator";
import { awaitingAnswer, filterWords, onAnswer, onPreviewResult, onSearchResult, serviceLabel, startTagFlow,
         type FlowStep, type TagFlow, type TagIntent } from "./lib/tagflow";
import { Activity } from "./pages/Activity";
import { Assistant } from "./pages/Assistant";
import { Cost } from "./pages/Cost";
import { Developer } from "./pages/Developer";
import { Governance } from "./pages/Governance";
import { Overview } from "./pages/Overview";
import { Settings } from "./pages/Settings";
import { Tagging, type TaggingRecord } from "./pages/Tagging";
import type { Page, PageCtx } from "./pages/types";
import { ConnectScreen } from "./components/ConnectScreen";
import { Sidebar, Topbar } from "./components/Shell";
import type { VoiceTurn } from "./pages/Assistant";

const DEFAULT_REGION = "ap-south-1";

// Same words the voice controller accepts for cancelling a pending region question.
const CANCEL = /^\s*(cancel|never ?mind|stop|forget it|no)\b/i;
/** True when the server advertises CloudGuard's Alexa projection (schema_version "alexa-1") for its tools. */
function isAlexaSurface(tools: Tool[]): boolean {
  return tools.length > 0 && tools.every((t) => {
    const props = (t.outputSchema as { properties?: Record<string, { default?: unknown }> } | undefined)?.properties;
    return props?.schema_version?.default === "alexa-1";
  });
}


let nextId = 1;

interface RunOutcome {
  id?: number;
  result?: CallToolResult;
  error?: SimulatorError;
  cached?: CachedAnswer<CallToolResult>; // set when the answer came from the cache (no server call)
}

// "use": answer from the cache when possible (typed and spoken questions). "refresh": always call the server
// (Refresh data). "bypass": the tool explorer's direct calls, always fresh. Every successful complete result is
// stored, whatever the mode.
type CacheMode = "use" | "refresh" | "bypass";
let connectionSeq = 0;

export default function App() {
  const [url, setUrl] = useState("/mcp");
  const [token, setToken] = useState(""); // memory only; cleared as soon as the connection is made
  const [conn, setConn] = useState<Connection | null>(null);
  const [mode, setMode] = useState<string | undefined>();
  const [status, setStatus] = useState<"disconnected" | "connecting" | "connected">("disconnected");
  const [connError, setConnError] = useState<SimulatorError | null>(null);
  const [connWarning, setConnWarning] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [preset, setPreset] = useState<Record<string, unknown> | undefined>();
  const [execs, setExecs] = useState<Execution[]>([]);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [question, setQuestion] = useState("");
  const [findings, setFindings] = useState<{ finding_id: string; label: string }[]>([]);
  // An open question (COLLECT_REGION or "which resource?"): the original request and, for a tagging request, its
  // parsed intent are kept until the answer completes them.
  const [pendingQ, setPendingQ] = useState<{ tool: string; args: Record<string, unknown>; reason: string;
                                              missing: "region" | "resource_name"; request?: string;
                                              tagIntent?: TagIntent } | null>(null);
  // DISPLAY_RESULTS: the last typed listing, so "What about Singapore?" runs it again in that region (a new call).
  const lastListing = useRef<{ tool: string; args: Record<string, unknown>; request: string } | null>(null);
  const [page, setPage] = useState<Page>("assistant");
  const [menuOpen, setMenuOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false); // sidebar; memory only, like every other setting here
  const [connected, setConnected] = useState<string | null>(null); // the connected-state confirmation
  const [voiceLog, setVoiceLog] = useState<VoiceTurn[]>([]); // this session's spoken exchanges, newest last
  const lastLogged = useRef({ transcript: "", message: "" });
  const [history, setHistory] = useState<TaggingRecord[]>([]); // voice tagging executions in this session
  const recorded = useRef(new Set<number>());
  const [region, setRegion] = useState(DEFAULT_REGION); // the region a tagging request uses when it names none
  const regionRef = useRef(DEFAULT_REGION);
  regionRef.current = region;
  // Personal voice tagging: the server's session status only (the session itself is an HttpOnly cookie).
  const [opSession, setOpSession] = useState<OperatorSession | null>(null);
  const opRef = useRef<OperatorSession | null>(null);
  opRef.current = opSession;
  const flowRef = useRef<TagFlow | null>(null); // the typed tagging conversation (the voice one is the controller's)
  const lastResource = useRef<{ finding_id: string; name?: string; service?: string } | null>(null);
  const connRef = useRef<Connection | null>(null);
  const namesRef = useRef<string[]>([]);
  const toolsRef = useRef<Tool[]>([]);
  const runRef = useRef<(name: string, args: Record<string, unknown>, mode?: CacheMode) => Promise<RunOutcome>>();
  // Conversation answer cache: memory only, scoped to the current connection, cleared on disconnect.
  const cache = useMemo(() => new AnswerCache<CallToolResult>(), []);
  // Calls in progress by tool + arguments, so the same question asked twice at once makes one server call.
  const inflight = useRef(new Map<string, Promise<RunOutcome>>());
  // One voice controller per page. It reaches the server only through run() (the same MCP pipeline as the buttons)
  // and never sees the token.
  const voice = useMemo(() => new VoiceController({
    ...browserVoiceDeps(),
    tools: () => namesRef.current,
    needsConfirmation: (name) => requiresConfirmation(toolsRef.current.find((t) => t.name === name)),
    lookup: (name, args) => {
      const hit = cache.get(name, args);
      if (!hit) return null;
      markReused(hit.execId);
      const said = spokenAnswer(name, args, hit.result.structuredContent as never);
      return { speech: said.speech ?? hit.speech, ageMs: Date.now() - hit.storedAt, resource: said.resource };
    },
    execute: async (name, args, opts): Promise<ExecuteResult> => {
      const out = await runRef.current!(name, args, opts?.refresh ? "refresh" : "use");
      if (out.error) return { errorText: out.error.message };
      if (out.result?.isError) {
        // "No scanned Lambda function has that name: a, b, c" is the answer itself: say it (the server sends only
        // safe display names in it, and never echoes what was asked).
        const text = (out.result.content?.[0] as { text?: unknown } | undefined)?.text;
        const named = (name === "explain_finding" || name === "recommend_tags") && typeof text === "string"
          ? text.replace(/^Error executing tool \w+: /, "") : undefined;
        return { isError: true, errorText: named ? `${named.charAt(0).toUpperCase()}${named.slice(1)}.`
          : "The tool reported an error; see the results panel." };
      }
      const said = spokenAnswer(name, args, out.result?.structuredContent as never);
      return { speech: said.speech, resource: said.resource };
    },
    call: async (name, args) => {
      const out = await runRef.current!(name, args, name === "preview_tag_changes" ? "bypass" : "refresh");
      if (out.error) return { errorText: out.error.message };
      if (out.result?.isError) return { errorText: "The tool reported an error; see the results panel." };
      return { structured: out.result?.structuredContent };
    },
    selectedRegion: () => regionRef.current,
    regionSupport: (name) => regionSupport(toolsRef.current.find((t) => t.name === name)),
    // Only the voice controller gets this, and only while an operator session is active: typed chat never writes.
    operator: () => (opRef.current?.authenticated && !opRef.current.kill_switch ? operatorApi : null),
    candidateNames: async (services) => {
      const out = await runRef.current!("list_untagged_resources", services ? { services } : {}, "use");
      const rows = (out.result?.structuredContent as { resources?: { display_name?: string | null }[] } | undefined)?.resources;
      return (rows ?? []).map((r) => r.display_name).filter((n): n is string => typeof n === "string" && n.length > 0);
    },
  }, undefined, { recognitionLang: defaultRecognitionLang() }), []);

  // On unmount: stop listening, speaking and any scheduled auto-listen. attach() makes this safe under StrictMode,
  // which runs the effect, its cleanup, and the effect again.
  useEffect(() => {
    const refresh = () => void fetchOperatorSession().then(setOpSession);
    refresh();
    window.addEventListener("focus", refresh);
    const timer = window.setInterval(refresh, 30_000); // notices expiry and the kill switch
    return () => {
      window.removeEventListener("focus", refresh);
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    voice.attach();
    return () => {
      voice.reset();
      voice.dispose();
    };
  }, [voice]);

  // Record each finished voice tagging execution once, with every matched resource: the server's per-resource
  // outcome, and for resources the plan left alone, the plan's own reason (display only; nothing is re-derived).
  const vs = useVoiceState(voice);
  useEffect(() => {
    const f = vs.writeFlow;
    if (!f || f.stage !== "done" || !f.results || recorded.current.has(f.id)) return;
    recorded.current.add(f.id);
    const results = f.results;
    const done = new Set(results.map((r) => r.name));
    const unchanged = (f.plan?.resources ?? []).filter((r) => !r.changes.length && !done.has(r.name))
      .map((r) => ({ name: r.name, category: "not_changed", status: unchangedReason(r), error: "NONE" }));
    setHistory((h) => [{ at: new Date(), service: f.service, region: f.region, tag: f.tag ? `${f.tag.key}=${f.tag.value}` : "",
                         outcome: f.outcome, results: [...results.map((r) => ({ name: r.name, category: r.category,
                         status: r.status, error: r.error })), ...unchanged] }, ...h]);
  }, [vs.writeFlow]);

  // The assistant's response history: each new reply, with the words it answered (when they are new).
  useEffect(() => {
    const m = vs.message;
    if (!m || m === PREVIEW_TEXT || m === FOLLOW_UP_TEXT || m === lastLogged.current.message) return;
    const you = vs.transcript && vs.transcript !== lastLogged.current.transcript ? vs.transcript : undefined;
    lastLogged.current = { transcript: vs.transcript, message: m };
    setVoiceLog((l) => [...l, { id: nextId++, at: new Date(), you, final: vs.transcriptFinal, reply: m,
                                stage: vs.discovery?.stage }].slice(-40));
  }, [vs.message]);

  const tools = conn?.tools ?? [];
  const names = useMemo(() => tools.map((t) => t.name), [tools]);
  namesRef.current = names;
  toolsRef.current = tools;
  const busy = execs.some((e) => e.state === "running");

  async function onConnect(e: React.FormEvent) {
    e.preventDefault();
    setStatus("connecting");
    setConnError(null);
    try {
      const c = await connect(url, token);
      if (!isAlexaSurface(c.tools)) {
        await c.close();
        throw new SimulatorError("auth", "This token does not receive CloudGuard's Alexa response projection. "
          + "The simulator only accepts the Alexa token (alexa:read); never use an operator token here.");
      }
      connRef.current = c;
      cache.setScope(`${url}#${++connectionSeq}`); // a new connection never sees another connection's answers
      setConn(c);
      setMode(await serverMode(url));
      setConnWarning(null);
      setStatus("connected");
      setSelected(null);
      setConnected(`Connected to ${c.serverName} ${c.serverVersion} · protocol ${c.protocolVersion ?? "not reported"} · `
        + `${c.tools.length} tools discovered with tools/list.`);
    } catch (err) {
      setStatus("disconnected");
      setConnError(err instanceof SimulatorError ? err : new SimulatorError("network", "Connection failed."));
    } finally {
      setToken(""); // the token now exists only inside the connection's closure (or nowhere, on failure)
    }
  }

  async function onDisconnect() {
    await connRef.current?.close();
    connRef.current = null;
    cache.setScope(null);
    cache.clear();
    inflight.current.clear();
    voice.reset();
    setConn(null);
    setStatus("disconnected");
    setMode(undefined);
    setConnWarning(null);
    setToken("");
    setExecs([]);
    setMsgs([]);
    setFindings([]);
    setPendingQ(null);
    flowRef.current = null;
    lastResource.current = null;
    lastListing.current = null;
    setSelected(null);
    setHistory([]);
    recorded.current.clear();
    setVoiceLog([]);
    lastLogged.current = { transcript: "", message: "" };
    setConnected(null);
    setPage("assistant");
  }

  function markReused(execId: number | undefined) {
    if (execId !== undefined) setExecs((xs) => xs.map((x) => (x.id === execId ? { ...x, reusedAt: new Date() } : x)));
  }

  async function run(name: string, args: Record<string, unknown>, mode: CacheMode = "use"): Promise<RunOutcome> {
    if (!connRef.current) return { error: new SimulatorError("disconnected", "Not connected.") };
    if (mode === "use") {
      const hit = cache.get(name, args);
      if (hit) {
        markReused(hit.execId);
        return { id: hit.execId, result: hit.result, cached: hit };
      }
    }
    if (mode === "bypass") return callServer(name, args);
    // Refresh data asks the server for a new scan too (refresh: true, where the tool takes it), not only past the
    // browser cache; the answer is still cached under the question's own arguments.
    const sent = mode === "refresh" ? withRefresh(connRef.current.tools.find((t) => t.name === name), args) : args;
    const key = `${mode}\u0000${name}\u0000${stableArgs(sent)}`;
    const running = inflight.current.get(key);
    if (running) return running;
    const p = callServer(name, args, sent).finally(() => inflight.current.delete(key));
    inflight.current.set(key, p);
    return p;
  }

  async function callServer(name: string, args: Record<string, unknown>, sent = args): Promise<RunOutcome> {
    const c = connRef.current;
    if (!c) return { error: new SimulatorError("disconnected", "Not connected.") };
    const id = nextId++;
    const started = performance.now();
    setExecs((xs) => [{ id, tool: name, args: sent, state: "running", startedAt: new Date() }, ...xs]);
    let patch: Partial<Execution>;
    try {
      const result: CallToolResult = await c.callTool(name, sent);
      patch = { state: result.isError ? "tool-error" : "ok", result };
      if (connRef.current === c) cache.put(name, args, result, id); // only complete, successful answers are kept
      const sc = result.structuredContent as { resources?: { finding_id: string; service: string; region: string; missing_tags?: string[] }[] } | undefined;
      if (!result.isError && name === "list_untagged_resources" && Array.isArray(sc?.resources)) {
        setFindings(sc!.resources.map((r, i) => ({
          finding_id: r.finding_id,
          label: `${i + 1}. ${r.service} · ${r.region} · missing ${(r.missing_tags ?? []).length}`,
        })));
      }
    } catch (err) {
      const error = err instanceof SimulatorError ? err : new SimulatorError("mcp", "Request failed.");
      patch = { state: "failed", error };
      if (["unavailable", "disconnected", "network", "timeout"].includes(error.kind)) {
        setConnWarning("The last request could not reach the server. Results below are only what the server "
          + "actually returned. Check the backend, then Disconnect and Connect again.");
      } else if (error.kind === "auth") {
        setConnWarning("The server no longer accepts this token. Disconnect and connect with a valid Alexa token.");
      }
    }
    const ms = Math.round(performance.now() - started);
    setExecs((xs) => xs.map((x) => (x.id === id ? { ...x, ...patch, ms } : x)));
    return { id, result: patch.result, error: patch.error };
  }
  runRef.current = run;

  /** Show one step of the typed tagging conversation, then run its one read-only call and show the next. */
  async function stepFlow(step: FlowStep, userText?: string) {
    flowRef.current = step.flow;
    const add: Msg[] = userText ? [{ id: nextId++, role: "user", text: userText }] : [];
    if (step.say) add.push({ id: nextId++, role: "assistant", text: step.say, flow: step.flow });
    setMsgs((m) => [...m, ...add]);
    setQuestion("");
    if (!step.call) return;
    const out = await run(step.call.tool, step.call.args, step.call.tool === "preview_tag_changes" ? "bypass" : "refresh");
    const flow = flowRef.current;
    if (!flow || flow.id !== step.flow.id) return; // disconnected or replaced meanwhile
    const errorText = out.error?.message ?? (out.result?.isError ? "the tool reported an error" : undefined);
    const sc = out.result?.structuredContent as never;
    const next = step.call.tool === "search_resources" ? onSearchResult(flow, sc, errorText)
      : onPreviewResult(flow, sc, errorText);
    void stepFlow(next);
  }

  function confirmFlow(version: number) {
    const flow = flowRef.current;
    if (!flow || flow.stage !== "confirm" || flow.version !== version) return; // a button for an older summary
    void stepFlow(onAnswer(flow, "yes", version), "Yes");
  }

  function submit(text: string) {
    const q = text.trim();
    if (!q) return;
    if (awaitingAnswer(flowRef.current)) {
      // The answer to "What key and value?" or "Shall I prepare this tagging proposal?" (yes only when explicit).
      void stepFlow(onAnswer(flowRef.current, q), q);
      return;
    }
    if (pendingQ && CANCEL.test(q)) {
      // Cancels the open "which region?" / "which resource?" question; nothing runs, and the next question is
      // handled normally.
      setPendingQ(null);
      setMsgs((m) => [...m, { id: nextId++, role: "user", text: q },
                      { id: nextId++, role: "assistant", text: "Okay, cancelled. Nothing was run." }]);
      setQuestion("");
      return;
    }
    let plan = mapIntent(q, names);
    const say = (text: string, extra: Partial<Msg> = {}) =>
      setMsgs((m) => [...m, { id: nextId++, role: "user", text: q }, { id: nextId++, role: "assistant", text, ...extra }]);
    const regionReply = pendingQ?.missing === "region" && regionOnly(q) !== undefined;
    if (plan.kind === "tag" && !regionReply) {
      setPendingQ(null);
      lastListing.current = null;
      if (!plan.intent.region) {
        // COLLECT_REGION before the search: the request names no region, and no default is ever assumed.
        const prompt = regionQuestion(`the ${serviceLabel(plan.intent.service)} with ${filterWords(plan.intent.filter)}`,
                                      { allRegions: false });
        const args = { text: plan.intent.filter.text, match: plan.intent.filter.match, services: [plan.intent.service] };
        setPendingQ({ tool: "search_resources", args, reason: plan.reason, missing: "region", request: q, tagIntent: plan.intent });
        say(plan.reason, { plan: { kind: "needs", tool: "search_resources", args, missing: "region", reason: plan.reason, prompt } });
        setQuestion("");
        return;
      }
      void stepFlow(startTagFlow(plan.intent, nextId++, plan.intent.region), q);
      return;
    }
    if (pendingQ?.tagIntent && (regionReply || (plan.kind !== "call" && plan.kind !== "needs" && plan.kind !== "tag"))) {
      // The region for a tagging request: one region only, then the existing search -> tag -> preview flow.
      const answer = regionAnswer(q, { allRegions: false, oneRegionReason: "Tagging is done one region at a time." });
      setQuestion("");
      if (answer.kind === "retry") {
        say(answer.prompt, { plan: { kind: "needs", tool: "search_resources", args: pendingQ.args, missing: "region",
                                     reason: pendingQ.reason, prompt: answer.prompt } });
        return;
      }
      const intent = { ...pendingQ.tagIntent, region: answer.region };
      setPendingQ(null);
      void stepFlow(startTagFlow(intent, nextId++, answer.region), q);
      return;
    }
    // DISPLAY_RESULTS: "What about Singapore?" after a listing runs that listing again in the new region.
    const change = !pendingQ && lastListing.current && plan.kind === "none" ? regionOnly(q) : undefined;
    if (change && change !== "ambiguous" && lastListing.current) {
      const last = lastListing.current;
      if (change === "all" && !regionSupport(tools.find((t) => t.name === last.tool)).allRegions) {
        plan = { kind: "none", reason: "This server can't search every region for that request. Name one region, such as Mumbai." };
      } else {
        const args = withRegion(last.tool, last.args, change);
        plan = { kind: "call", tool: last.tool, args, reason: searchingText(last.tool, args) };
      }
    }
    // An open question takes this as its answer ("Mumbai", "OrderProcessor"), unless it is a complete question of
    // its own ("Show my Lambda functions in Mumbai"). A short answer to "Which Lambda function?" is always a name.
    if (pendingQ) {
      const shortName = pendingQ.missing === "resource_name" && q.split(/\s+/).length <= 4 && !q.includes("?");
      if (regionReply || shortName || (plan.kind !== "call" && plan.kind !== "needs")) {
        plan = pendingQ.missing === "resource_name" ? answerResourceName(pendingQ, q)
          : answerRegion(pendingQ, q, regionSupport(tools.find((t) => t.name === pendingQ.tool)));
        if (plan.kind === "call" && pendingQ.missing === "region") plan = { ...plan, reason: searchingText(plan.tool, plan.args) };
      }
    }
    const request = pendingQ?.request ?? q;
    if (plan.kind === "call" && isListing(plan.tool)) lastListing.current = { tool: plan.tool, args: plan.args, request };
    else if (plan.kind !== "none") lastListing.current = null;
    // "What tags should I add?" right after explaining a resource means that resource.
    const last = lastResource.current;
    if (plan.kind === "needs" && plan.missing === "resource_name" && last
        && (!plan.args.service || plan.args.service === last.service)) {
      plan = { kind: "call", tool: plan.tool, reason: plan.reason,
               args: plan.tool === "explain_finding" ? { finding_id: last.finding_id } : { resource: last.finding_id } };
    }
    setPendingQ(plan.kind === "needs" && (plan.missing === "region" || plan.missing === "resource_name")
      ? { tool: plan.tool, args: plan.args, reason: plan.reason, missing: plan.missing, request } : null);
    // A question already answered recently shows that answer at once; reading the cache calls no tool.
    const hit = plan.kind === "call" ? cache.get(plan.tool, plan.args) : null;
    if (hit) markReused(hit.execId);
    const reply: Msg = hit
      ? { id: nextId++, role: "assistant", text: plan.reason, plan, execId: hit.execId, cached: { age: ageText(Date.now() - hit.storedAt) } }
      : { id: nextId++, role: "assistant", text: plan.reason, plan };
    if (hit && plan.kind === "call") {
      const said = spokenAnswer(plan.tool, plan.args, hit.result.structuredContent as never);
      reply.answer = said.speech;
      if (said.resource) lastResource.current = said.resource;
    }
    setMsgs((m) => [...m, { id: nextId++, role: "user", text: q }, reply]);
    setQuestion("");
    // "Which Lambda function?": offer the names of resources that need tags (a read-only, cached call).
    if (plan.kind === "needs" && plan.missing === "resource_name") {
      const services = plan.args.service ? [plan.args.service as string] : undefined;
      void run("list_untagged_resources", services ? { services } : {}, "use").then((out) => {
        const rows = (out.result?.structuredContent as { resources?: { display_name?: string | null }[] } | undefined)?.resources;
        const options = (rows ?? []).map((r) => r.display_name).filter((n): n is string => !!n).slice(0, 10);
        setMsgs((m) => m.map((x) => (x.id === reply.id ? { ...x, options } : x)));
      });
    }
    // Read-only tools run at once; anything else waits for "Confirm and call".
    if (plan.kind === "call" && !hit && !requiresConfirmation(tools.find((t) => t.name === plan.tool))) void confirm(reply);
  }

  async function confirm(msg: Msg, mode: CacheMode = "use") {
    if (msg.plan?.kind !== "call") return;
    const out = await run(msg.plan.tool, msg.plan.args, mode);
    const cached = out.cached ? { age: ageText(Date.now() - out.cached.storedAt) } : undefined;
    const said = out.result && !out.result.isError
      ? spokenAnswer(msg.plan.tool, msg.plan.args, out.result.structuredContent as never) : {};
    if (said.resource) lastResource.current = said.resource;
    setMsgs((m) => m.map((x) => (x.id === msg.id ? { ...x, execId: out.id, cached, answer: said.speech } : x)));
  }

  function openInExplorer(name: string, args: Record<string, unknown>) {
    setSelected(name);
    setPreset(args);
    setPage("developer");
  }

  const operatorCard = (
    <OperatorCard session={opSession} region={region}
      onSignOut={() => void logoutOperator().then(() => { voice.reset(); return fetchOperatorSession(); }).then(setOpSession)}
      onKill={() => void engageKillSwitch().then(() => { voice.reset(); return fetchOperatorSession(); }).then(setOpSession)} />
  );
  const conversation = (
    <Conversation msgs={msgs} execs={execs} busy={busy} question={question} setQuestion={setQuestion} submit={submit}
      confirm={(m, mode) => void confirm(m, mode)} confirmFlow={confirmFlow} currentFlow={flowRef.current}
      openInExplorer={openInExplorer} regionChoices={regionChoices(region)} />
  );
  const go = (p: Page) => { setPage(p); setMenuOpen(false); };
  const ctx: PageCtx = { run: run as PageCtx["run"], names, mode, region, go };

  const badges: Partial<Record<Page, { text: string; tone: string }>> = {
    ...(opSession?.kill_switch ? { tagging: { text: "off", tone: "bad" } } : {}),
    ...(execs.length ? { activity: { text: String(execs.length), tone: "muted-pill" } } : {}),
  };

  return (
    <div className={`app ${collapsed ? "is-collapsed" : ""} ${conn ? "" : "is-disconnected"}`}>
      <Sidebar page={page} go={go} status={status} conn={conn} session={opSession} collapsed={collapsed}
               setCollapsed={setCollapsed} open={menuOpen} badges={badges} />

      <div className="main">
        {conn && <Topbar page={page} conn={conn} mode={mode} region={region} setRegion={setRegion} session={opSession}
                         menuOpen={menuOpen} setMenuOpen={setMenuOpen} onDisconnect={() => void onDisconnect()} />}
        {conn && (
          <div className={`mode-banner ${mode === "fixture" ? "fixture" : mode === "live" ? "live" : "unknown"}`} role="status">
            {mode === "fixture" ? "FIXTURE MODE — synthetic demo data. No AWS account is read and no AWS API is called."
              : mode === "live" ? "LIVE MODE — real data from the server's AWS account (read-only). Cost Explorer requests are billed by AWS."
              : "MODE UNKNOWN — /healthz could not be read. Treat these results as if they came from a real AWS account."}
          </div>
        )}
        {conn && connected && (
          <div className="connected-banner" role="status">
            <span className="dot ok" aria-hidden />{connected}
            <button className="link" onClick={() => setConnected(null)} aria-label="Dismiss connection message">Dismiss</button>
          </div>
        )}
        {connWarning && <div className="conn-warning" role="alert">{connWarning}</div>}
        {!conn ? (
          <ConnectScreen url={url} setUrl={setUrl} token={token} setToken={setToken} status={status} error={connError}
                         onConnect={(e) => void onConnect(e)} operator={opSession ? operatorCard : undefined} />
        ) : page === "overview" ? <Overview {...ctx} />
          : page === "cost" ? <Cost {...ctx} />
          : page === "governance" ? <Governance {...ctx} />
          : page === "tagging" ? <Tagging {...ctx} voice={voice} operator={operatorCard} history={history} />
          : page === "activity" ? <Activity execs={execs} history={history} voiceLog={voiceLog} />
          : page === "settings" ? <Settings conn={conn} url={url} mode={mode} names={names} region={region} setRegion={setRegion}
                                            session={opSession} operator={operatorCard} onClearCache={() => cache.clear()} onDisconnect={() => void onDisconnect()} />
          : page === "developer" ? <Developer tools={tools} selected={selected} preset={preset} busy={busy} findings={findings} execs={execs}
                                              open={(n, a) => { setSelected(n); setPreset(a); }} call={(n, a) => run(n, a, "bypass")} />
          : <Assistant voice={voice} conversation={conversation} operator={operatorCard} execs={execs} voiceLog={voiceLog} />}
      </div>
      {menuOpen && <div className="scrim" onClick={() => setMenuOpen(false)} aria-hidden />}
    </div>
  );
}
