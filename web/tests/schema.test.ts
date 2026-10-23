import { describe, expect, it } from "vitest";

import { argsFromForm, argsFromJson, fields, validate, type JsonSchema } from "../src/lib/schema";

// Shapes copied from the server's advertised inputSchema (pydantic/FastMCP output).
const MONTH = { type: "string", maxLength: 10, pattern: "^(\\d{4}-\\d{2}|last-month|this-month|[A-Za-z]{3,9})$" };
const BREAKDOWN: JsonSchema = {
  type: "object", additionalProperties: false,
  properties: { month: { ...MONTH, default: "last-month" }, limit: { type: "integer", minimum: 1, maximum: 50, default: 10 } },
};
const EXPLAIN: JsonSchema = {
  type: "object", additionalProperties: false, required: ["finding_id"],
  properties: { finding_id: { type: "string", pattern: "^tagcomp-[0-9a-f]{16}$" } },
};
const UNTAGGED: JsonSchema = {
  type: "object", additionalProperties: false,
  properties: { cursor: { anyOf: [{ type: "string", pattern: "^scan-\\d{8}T\\d{6}Z-[0-9a-f]{6}:\\d{1,6}$" }, { type: "null" }], default: null } },
};
const NESTED: JsonSchema = {
  type: "object", required: ["filter"],
  properties: { filter: { type: "object", required: ["tags"], properties: { tags: { type: "array", items: { type: "string" } } } } },
};

describe("fields", () => {
  it("derives kinds, required and nullable from the schema", () => {
    const [m, l] = fields(BREAKDOWN);
    expect([m.name, m.kind, m.required, m.defaultValue]).toEqual(["month", "string", false, "last-month"]);
    expect([l.kind, l.required]).toEqual(["integer", false]);
    const [c] = fields(UNTAGGED);
    expect([c.kind, c.nullable]).toEqual(["string", true]);
    expect(fields(NESTED)[0].kind).toBe("json");
  });
});

describe("validation before any request", () => {
  it("omits empty optional fields instead of inventing values", () => {
    expect(argsFromForm(fields(BREAKDOWN), { month: "", limit: "" })).toEqual({ args: {}, errors: [] });
  });

  it("rejects missing required fields", () => {
    expect(argsFromForm(fields(EXPLAIN), {}).errors).toEqual(["finding_id: required"]);
    expect(argsFromJson("{}", EXPLAIN).errors).toEqual(["arguments.finding_id: required"]);
  });

  it("checks basic JSON types, ranges and patterns", () => {
    expect(argsFromForm(fields(BREAKDOWN), { limit: "ten" }).errors).toEqual(["limit: expected an integer"]);
    expect(validate({ limit: 51 }, BREAKDOWN)).toEqual(["arguments.limit: must be ≤ 50"]);
    expect(validate({ limit: 1.5 }, BREAKDOWN)).toEqual(["arguments.limit: expected integer"]);
    expect(validate({ month: "last week!" }, BREAKDOWN)).toContain("arguments.month: invalid format");
    expect(validate({ finding_id: "tagcomp-XYZ" }, EXPLAIN)).toEqual(["arguments.finding_id: invalid format"]);
    expect(validate({ cursor: null }, UNTAGGED)).toEqual([]);
    expect(validate({ cursor: 3 }, UNTAGGED)).toEqual(["arguments.cursor: does not match any allowed form"]);
  });

  it("rejects unknown arguments and invalid JSON", () => {
    expect(argsFromJson('{"account_id": "1"}', BREAKDOWN).errors).toEqual(["arguments.account_id: not accepted by this tool"]);
    expect(argsFromJson("{month: x}", BREAKDOWN).errors[0]).toMatch(/^invalid JSON/);
    expect(argsFromJson("[1]", BREAKDOWN).errors).toEqual(["arguments must be a JSON object"]);
  });

  it("validates nested objects and arrays", () => {
    expect(argsFromJson('{"filter": {"tags": ["a", 1]}}', NESTED).errors).toEqual(["arguments.filter.tags[1]: expected string"]);
    expect(argsFromJson('{"filter": {}}', NESTED).errors).toEqual(["arguments.filter.tags: required"]);
    expect(argsFromJson('{"filter": {"tags": ["a"]}}', NESTED).errors).toEqual([]);
  });
});
