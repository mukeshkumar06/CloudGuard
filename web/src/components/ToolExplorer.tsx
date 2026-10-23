import type { Tool } from "@modelcontextprotocol/sdk/types.js";
import { useEffect, useMemo, useState } from "react";

import { argsFromForm, argsFromJson, fields, type JsonSchema } from "../lib/schema";
import { readOnly, summary, title } from "../lib/tools";

interface Props {
  tool: Tool;
  preset?: Record<string, unknown>;
  busy: boolean;
  findings: { finding_id: string; label: string }[];
  onRun: (args: Record<string, unknown>) => void;
}

export function ToolExplorer({ tool, preset, busy, findings, onRun }: Props) {
  const schema = tool.inputSchema as JsonSchema;
  const fs = useMemo(() => fields(schema), [schema]);
  const [values, setValues] = useState<Record<string, string>>({});
  const [raw, setRaw] = useState(false);
  const [rawText, setRawText] = useState("{}");
  const [errors, setErrors] = useState<string[]>([]);

  useEffect(() => {
    const init: Record<string, string> = {};
    for (const [k, v] of Object.entries(preset ?? {})) init[k] = typeof v === "string" ? v : JSON.stringify(v);
    setValues(init);
    setRawText(JSON.stringify(preset ?? {}, null, 2));
    setErrors([]);
  }, [tool.name, preset]);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const { args, errors: errs } = raw ? argsFromJson(rawText, schema) : argsFromForm(fs, values);
    const all = raw ? errs : [...errs, ...(errs.length ? [] : argsFromJson(JSON.stringify(args), schema).errors)];
    setErrors(all);
    if (all.length === 0) onRun(args); // invalid input never reaches the network
  }

  function toggleRaw() {
    if (!raw) setRawText(JSON.stringify(argsFromForm(fs, values).args, null, 2));
    setRaw(!raw);
    setErrors([]);
  }

  const findingField = fs.find((f) => f.name === "finding_id" || f.name === "resource");
  return (
    <form className="explorer" onSubmit={submit} noValidate>
      <header>
        <h2>{title(tool)} <code className="small">{tool.name}</code></h2>
        <span className={`pill ${readOnly(tool) === "read-only" ? "ok" : "warn"}`} title="From the server's tool annotations">
          {readOnly(tool)}
        </span>
      </header>
      <p className="description">{summary(tool)}</p>
      <details><summary>Full description</summary><pre className="wrap">{tool.description}</pre></details>
      <details><summary>Input schema (from tools/list)</summary><pre>{JSON.stringify(tool.inputSchema, null, 2)}</pre></details>

      <div className="mode-switch">
        <button type="button" className="link" onClick={toggleRaw}>{raw ? "Use form" : "Edit raw JSON"}</button>
      </div>

      {raw ? (
        <label className="field">
          <span>Arguments (JSON object)</span>
          <textarea value={rawText} onChange={(e) => setRawText(e.target.value)} rows={6} spellCheck={false} />
        </label>
      ) : fs.length === 0 ? (
        <p className="muted">This tool takes no arguments.</p>
      ) : (
        fs.map((f) => (
          <label className="field" key={f.name}>
            <span>
              {f.name}{f.required ? <b className="req" title="required"> *</b> : <span className="muted small"> optional</span>}
              {f.defaultValue !== undefined && f.defaultValue !== null && <span className="muted small"> · server default {JSON.stringify(f.defaultValue)}</span>}
            </span>
            {f.kind === "boolean" ? (
              <select value={values[f.name] ?? ""} onChange={(e) => setValues({ ...values, [f.name]: e.target.value })}>
                <option value="">(omit)</option><option value="true">true</option><option value="false">false</option>
              </select>
            ) : (
              <input
                value={values[f.name] ?? ""}
                inputMode={f.kind === "integer" || f.kind === "number" ? "numeric" : undefined}
                placeholder={f.kind === "json" ? "JSON value" : f.required ? "required" : "(omit to use the server default)"}
                onChange={(e) => setValues({ ...values, [f.name]: e.target.value })}
                autoComplete="off" spellCheck={false}
              />
            )}
            {f.description && <small className="muted">{f.description}</small>}
          </label>
        ))
      )}

      {findingField && !raw && findings.length > 0 && (
        <div className="picker">
          <span className="muted small">Finding IDs returned by the last <code>list_untagged_resources</code> call:</span>
          <div className="chips">
            {findings.map((f) => (
              <button type="button" key={f.finding_id} className="chip" onClick={() => setValues({ ...values, [findingField.name]: f.finding_id })}>
                {f.label}
              </button>
            ))}
          </div>
        </div>
      )}

      {errors.length > 0 && (
        <ul className="errors" role="alert">{errors.map((e) => <li key={e}>{e}</li>)}</ul>
      )}
      <button type="submit" className="primary" disabled={busy}>{busy ? "Calling…" : `Call ${tool.name}`}</button>
    </form>
  );
}
