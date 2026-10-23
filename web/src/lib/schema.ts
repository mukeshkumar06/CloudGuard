// Input forms and validation derived from a tool's advertised JSON Schema (inputSchema). Validation runs before
// any network request; the server validates again. Nothing is invented: an empty optional field is omitted, so
// the server applies its own default.

export type JsonSchema = {
  type?: string | string[];
  properties?: Record<string, JsonSchema>;
  required?: string[];
  items?: JsonSchema;
  anyOf?: JsonSchema[];
  enum?: unknown[];
  pattern?: string;
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  default?: unknown;
  description?: string;
  title?: string;
  additionalProperties?: boolean | JsonSchema;
};

export type FieldKind = "string" | "integer" | "number" | "boolean" | "json";

export interface Field {
  name: string;
  kind: FieldKind;
  required: boolean;
  nullable: boolean;
  schema: JsonSchema; // the non-null branch
  description?: string;
  defaultValue?: unknown;
}

function types(s: JsonSchema): string[] {
  return s.type === undefined ? [] : Array.isArray(s.type) ? s.type : [s.type];
}

/** The non-null branch of `anyOf: [X, {type: null}]`, and whether null is allowed. */
export function unwrap(s: JsonSchema): { schema: JsonSchema; nullable: boolean } {
  if (s.anyOf) {
    const rest = s.anyOf.filter((b) => !types(b).includes("null"));
    const nullable = rest.length < s.anyOf.length;
    if (rest.length === 1) return { schema: { ...rest[0], description: rest[0].description ?? s.description }, nullable };
    return { schema: s, nullable };
  }
  const t = types(s);
  return { schema: s, nullable: t.includes("null") };
}

export function fields(input: JsonSchema | undefined): Field[] {
  const props = input?.properties ?? {};
  const required = new Set(input?.required ?? []);
  return Object.entries(props).map(([name, raw]) => {
    const { schema, nullable } = unwrap(raw);
    const t = types(schema).filter((x) => x !== "null");
    const kind: FieldKind = t.length === 1 && ["string", "integer", "number", "boolean"].includes(t[0])
      ? (t[0] as FieldKind) : "json";
    return { name, kind, required: required.has(name), nullable, schema,
             description: schema.description ?? raw.description, defaultValue: raw.default };
  });
}

function typeOk(value: unknown, t: string): boolean {
  switch (t) {
    case "string": return typeof value === "string";
    case "integer": return typeof value === "number" && Number.isInteger(value);
    case "number": return typeof value === "number" && Number.isFinite(value);
    case "boolean": return typeof value === "boolean";
    case "null": return value === null;
    case "array": return Array.isArray(value);
    case "object": return typeof value === "object" && value !== null && !Array.isArray(value);
    default: return true;
  }
}

/** Errors for `value` against `schema` (a practical subset of JSON Schema), with JSON-pointer-like paths. */
export function validate(value: unknown, schema: JsonSchema, path = "arguments"): string[] {
  if (schema.anyOf) {
    const ok = schema.anyOf.some((b) => validate(value, b, path).length === 0);
    return ok ? [] : [`${path}: does not match any allowed form`];
  }
  const t = types(schema);
  if (t.length && !t.some((x) => typeOk(value, x))) return [`${path}: expected ${t.join(" or ")}`];
  const errors: string[] = [];
  if (schema.enum && !schema.enum.some((e) => JSON.stringify(e) === JSON.stringify(value))) {
    errors.push(`${path}: must be one of ${schema.enum.map((e) => JSON.stringify(e)).join(", ")}`);
  }
  if (typeof value === "string") {
    if (schema.minLength !== undefined && value.length < schema.minLength) errors.push(`${path}: too short`);
    if (schema.maxLength !== undefined && value.length > schema.maxLength) {
      errors.push(`${path}: at most ${schema.maxLength} characters`);
    }
    if (schema.pattern && !new RegExp(schema.pattern, "u").test(value)) errors.push(`${path}: invalid format`);
  }
  if (typeof value === "number") {
    if (schema.minimum !== undefined && value < schema.minimum) errors.push(`${path}: must be ≥ ${schema.minimum}`);
    if (schema.maximum !== undefined && value > schema.maximum) errors.push(`${path}: must be ≤ ${schema.maximum}`);
  }
  if (Array.isArray(value) && schema.items) {
    value.forEach((item, i) => errors.push(...validate(item, schema.items!, `${path}[${i}]`)));
  }
  if (typeOk(value, "object") && (schema.properties || schema.required || schema.additionalProperties === false)) {
    const obj = value as Record<string, unknown>;
    for (const r of schema.required ?? []) if (!(r in obj)) errors.push(`${path}.${r}: required`);
    for (const [k, v] of Object.entries(obj)) {
      const sub = schema.properties?.[k];
      if (sub) errors.push(...validate(v, sub, `${path}.${k}`));
      else if (schema.additionalProperties === false) errors.push(`${path}.${k}: not accepted by this tool`);
    }
  }
  return errors;
}

/** Form text -> arguments. Empty optional fields are omitted (the server's default applies). */
export function argsFromForm(fs: Field[], values: Record<string, string>): { args: Record<string, unknown>; errors: string[] } {
  const args: Record<string, unknown> = {};
  const errors: string[] = [];
  for (const f of fs) {
    const text = (values[f.name] ?? "").trim();
    if (text === "") {
      if (f.required) errors.push(`${f.name}: required`);
      continue;
    }
    if (f.kind === "string") args[f.name] = text;
    else if (f.kind === "boolean") {
      if (text !== "true" && text !== "false") errors.push(`${f.name}: expected true or false`);
      else args[f.name] = text === "true";
    } else if (f.kind === "integer" || f.kind === "number") {
      const n = Number(text);
      if (!/^-?\d+(\.\d+)?$/.test(text) || !Number.isFinite(n)) errors.push(`${f.name}: expected ${f.kind === "integer" ? "an integer" : "a number"}`);
      else args[f.name] = n;
    } else {
      try {
        args[f.name] = JSON.parse(text);
      } catch {
        errors.push(`${f.name}: invalid JSON`);
      }
    }
  }
  return { args, errors };
}

/** Raw JSON text -> arguments, rejected before any request if it isn't a JSON object valid for the schema. */
export function argsFromJson(text: string, input: JsonSchema | undefined): { args: Record<string, unknown>; errors: string[] } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text.trim() === "" ? "{}" : text);
  } catch (e) {
    return { args: {}, errors: [`invalid JSON: ${(e as Error).message}`] };
  }
  if (!typeOk(parsed, "object")) return { args: {}, errors: ["arguments must be a JSON object"] };
  const args = parsed as Record<string, unknown>;
  return { args, errors: input ? validate(args, input) : [] };
}
