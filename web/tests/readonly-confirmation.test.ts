// Which calls ask "Shall I continue?". Read-only tools run as soon as a question is matched; any other tool (none is on
// CloudGuard's MCP surface today; a custom-tagging tool would be one) needs its own explicit yes. Fake speech APIs, a
// spy for `execute`: no browser, no network, no AWS.
import type { Tool } from "@modelcontextprotocol/sdk/types.js";
import { describe, expect, it, vi } from "vitest";

import { READ_ONLY_TOOLS, requiresConfirmation } from "../src/lib/tools";
import { VoiceController, type RecognitionLike, type VoiceDeps } from "../src/lib/voice";

const ALL_TOOLS = [...READ_ONLY_TOOLS];
const flush = () => new Promise((r) => setTimeout(r, 0));

class FakeRecognition implements RecognitionLike {
  lang = "";
  interimResults = false;
  continuous = true;
  maxAlternatives = 0;
  onresult: RecognitionLike["onresult"] = null;
  onerror: RecognitionLike["onerror"] = null;
  onend: RecognitionLike["onend"] = null;
  onstart: RecognitionLike["onstart"] = null;
  start() { this.onstart?.(); }
  stop() { this.onend?.(); }
  abort() { this.onerror?.({ error: "aborted" }); this.onend?.(); }
  say(text: string) {
    this.onresult?.({ resultIndex: 0, results: [Object.assign([{ transcript: text }], { isFinal: true })] });
    this.onend?.();
  }
}

/** `stateChanging`: tools the app treats as changing the account (stand-ins for a custom-tagging tool). */
function setup(stateChanging: string[] = []) {
  const recs: FakeRecognition[] = [];
  const execute = vi.fn<VoiceDeps["execute"]>(async () => ({ speech: "Done." }));
  const c = new VoiceController({
    createRecognition: () => { const r = new FakeRecognition(); recs.push(r); return r; },
    synth: null, createUtterance: null, // muted: prompts complete at once
    tools: () => ALL_TOOLS,
    needsConfirmation: (t) => stateChanging.includes(t),
    execute,
  }, undefined, { autoListen: false });
  const say = async (text: string) => {
    c.startListening();
    recs[recs.length - 1].say(text);
    await flush();
  };
  return { c, execute, say };
}

const tool = (name: string, annotations?: Tool["annotations"]): Tool =>
  ({ name, inputSchema: { type: "object" }, annotations }) as Tool;
const RO = { readOnlyHint: true, destructiveHint: false };

describe("requiresConfirmation (fails closed)", () => {
  it("is false for each of the 13 known read-only tools declared read-only by the server", () => {
    expect(READ_ONLY_TOOLS.size).toBe(13);
    for (const name of READ_ONLY_TOOLS) expect(requiresConfirmation(tool(name, RO))).toBe(false);
  });
  it("recommend_tags is read-only: it suggests tags and never applies them", () => {
    expect(requiresConfirmation(tool("recommend_tags", RO))).toBe(false);
  });
  it("is true for a tagging or unknown tool, even one the server calls read-only", () => {
    expect(requiresConfirmation(tool("apply_tags", { readOnlyHint: false, destructiveHint: false }))).toBe(true);
    expect(requiresConfirmation(tool("apply_tags", RO))).toBe(true);
  });
  it("is true for a known tool whose server annotation is missing or not read-only", () => {
    expect(requiresConfirmation(tool("get_monthly_aws_bill"))).toBe(true);
    expect(requiresConfirmation(tool("get_monthly_aws_bill", { readOnlyHint: true, destructiveHint: true }))).toBe(true);
    expect(requiresConfirmation(undefined)).toBe(true);
  });
});

describe("read-only questions run without asking", () => {
  it.each([
    ["Which AWS account am I using?", "get_aws_account_identity"],
    ["What was my AWS bill last month?", "get_monthly_aws_bill"],
    ["How many AWS resources do I have in Mumbai?", "get_resource_inventory"],
    ["Check my tagging compliance.", "scan_tag_compliance"],
    ["Show me untagged resources in us-east-1.", "list_untagged_resources"],
  ])("%s -> %s, with no confirmation prompt", async (q, expected) => {
    const { c, execute, say } = setup();
    await say(q);
    expect(execute).toHaveBeenCalledTimes(1);
    expect(execute.mock.calls[0][0]).toBe(expected);
    expect(c.snapshot.pending).toBeNull();
    expect(c.snapshot.message).not.toMatch(/shall i continue|proceed\?/i);
  });

  it("recommend_tags runs without confirmation", async () => {
    const { c, execute, say } = setup();
    await say("Recommend tags for tagcomp-0123456789abcdef");
    expect(execute).toHaveBeenCalledWith("recommend_tags", { resource: "tagcomp-0123456789abcdef" });
    expect(c.snapshot.pending).toBeNull();
  });

  it("without needsConfirmation every call still asks (fails closed)", async () => {
    const execute = vi.fn<VoiceDeps["execute"]>(async () => ({ speech: "x" }));
    const c = new VoiceController({ createRecognition: null, synth: null, createUtterance: null,
                                    tools: () => ALL_TOOLS, execute }, undefined, { autoListen: false });
    await c.handleTranscript("What was my AWS bill last month?");
    expect(execute).not.toHaveBeenCalled();
    expect(c.snapshot.pending?.tool).toBe("get_monthly_aws_bill");
  });

  it("a tagging request is explained and never routed to a tool", async () => {
    const { c, execute, say } = setup();
    await say("Tag all my Lambda functions with Project=X");
    expect(execute).not.toHaveBeenCalled();
    expect(c.snapshot.pending).toBeNull();
  });
});

describe("state-changing calls need their own explicit yes", () => {
  const CHANGING = "get_monthly_aws_bill"; // stand-in: the app marks it state-changing for these tests
  const Q = "What was my AWS bill last month?";

  it("asks first and runs nothing until answered", async () => {
    const { c, execute, say } = setup([CHANGING]);
    await say(Q);
    expect(execute).not.toHaveBeenCalled();
    expect(c.snapshot.status).toBe("awaiting_confirmation");
    expect(c.snapshot.message).toMatch(/Shall I continue\?$/);
  });

  it.each(["Yes", "Yes, proceed.", "Go ahead!", "Confirm", "um, yes please", "Okay, yes, go ahead."])(
    "a clear yes (%s) runs the pending call once", async (answer) => {
      const { execute, say } = setup([CHANGING]);
      await say(Q);
      await say(answer);
      expect(execute).toHaveBeenCalledTimes(1);
      expect(execute.mock.calls[0][0]).toBe(CHANGING);
    });

  it.each(["No", "Cancel", "Don't do it.", "No thanks", "Yes, but don't run it", "yes no", "No, yes"])(
    "a refusal (%s) prevents execution, and refusal wins over yes", async (answer) => {
      const { c, execute, say } = setup([CHANGING]);
      await say(Q);
      await say(answer);
      expect(execute).not.toHaveBeenCalled();
      expect(c.snapshot.pending).toBeNull();
    });

  it.each(["maybe", "yes, but check this month instead", "what?", "hmm"])(
    "ambiguous speech (%s) asks again and never runs", async (answer) => {
      const { c, execute, say } = setup([CHANGING]);
      await say(Q);
      const id = c.snapshot.pending!.id;
      await say(answer);
      expect(execute).not.toHaveBeenCalled();
      expect(c.snapshot.pending?.id).toBe(id);
      expect(c.snapshot.message).toMatch(/^Sorry, I didn't catch a yes or no\./);
    });

  it("an empty or silent transcript never authorizes", async () => {
    const { c, execute, say } = setup([CHANGING]);
    await say(Q);
    await say("   ");
    await say("");
    expect(execute).not.toHaveBeenCalled();
    expect(c.snapshot.status).toBe("listening"); // still listening for a real answer
    c.stopListening();
    expect(execute).not.toHaveBeenCalled();
    expect(c.snapshot.status).toBe("awaiting_confirmation");
  });

  it("each state-changing call needs its own yes", async () => {
    const { c, execute, say } = setup([CHANGING]);
    await say(Q);
    await say("yes");
    expect(execute).toHaveBeenCalledTimes(1);
    await say(Q);
    expect(execute).toHaveBeenCalledTimes(1); // the earlier yes does not carry over
    expect(c.snapshot.status).toBe("awaiting_confirmation");
    await say("yes");
    expect(execute).toHaveBeenCalledTimes(2);
  });
});
