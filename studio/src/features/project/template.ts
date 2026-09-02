/** Pure helpers that turn a one-line command into the strict
 *  `run-template-spec-v2` shape (整頓 U4b). Mirrors the backend rules in
 *  `app/project_bootstrap.py` (`ArgvTemplateToken`, `RunParameterSpec`,
 *  `canonical_decimal`) so most mistakes are caught before the request. */

export type ParameterType = "string" | "integer" | "number" | "boolean" | "enum";

export interface ArgvToken {
  kind: "literal" | "parameter";
  value?: string;
  name?: string;
}

export interface ParameterSpec {
  name: string;
  type: ParameterType;
  required: true;
  min_length?: number;
  max_length?: number;
  minimum?: number;
  maximum?: number;
  enum_values?: string[];
  sensitive: false;
}

export interface CompiledTemplate {
  argv_template: ArgvToken[];
  parameter_schema: ParameterSpec[];
  errors: string[];
}

const IDENTIFIER_RE = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;
// eslint-disable-next-line no-control-regex
const CONTROL_RE = /[\x00-\x1f\x7f-\x9f]/;
const PARAMETER_RE = /^\{([^{}]+)\}$/;
const CANONICAL_DECIMAL_RE = /^-?(?:0|[1-9][0-9]*)\.[0-9]*[1-9]$/;
const INTEGER_RE = /^-?(?:0|[1-9][0-9]*)$/;

function specFor(name: string, type: ParameterType, enumValues?: string[]): ParameterSpec {
  const base = { name, required: true as const, sensitive: false as const };
  switch (type) {
    case "string":
      return { ...base, type, min_length: 0, max_length: 4096 };
    case "integer":
      return { ...base, type, minimum: -2147483648, maximum: 2147483647 };
    case "number":
      return { ...base, type, minimum: -1000000000, maximum: 1000000000 };
    case "boolean":
      return { ...base, type };
    case "enum":
      return { ...base, type, enum_values: Array.from(new Set(enumValues ?? [])).sort() };
  }
}

/** `python train.py --lr {lr:number} --mode {mode:enum(a|b)} --tag {tag}` →
 *  literal/parameter argv tokens plus a parameter schema. Parameters must be
 *  whole arguments (the backend argv is shell-free: no `--lr={lr}`). */
export function compileCommandTemplate(command: string): CompiledTemplate {
  const errors: string[] = [];
  if (CONTROL_RE.test(command.replace(/\n/g, ""))) errors.push("指令含有控制字元");
  const tokens = command.trim().split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return { argv_template: [], parameter_schema: [], errors: ["指令不能是空的"] };
  const argv: ArgvToken[] = [];
  const schema: ParameterSpec[] = [];
  const seen = new Set<string>();
  tokens.forEach((token, index) => {
    const match = PARAMETER_RE.exec(token);
    if (!match) {
      if (token.includes("{") || token.includes("}")) errors.push(`參數必須是獨立的引數：「${token}」（寫成 --lr {lr:number}）`);
      argv.push({ kind: "literal", value: token });
      return;
    }
    if (index === 0) errors.push("第一個引數必須是可執行檔，不能是參數");
    const [rawName, rawType = "string"] = match[1].split(":", 2);
    const name = rawName.trim();
    if (!IDENTIFIER_RE.test(name)) errors.push(`參數名稱「${name}」必須是識別字（英文、數字、底線，最長 64）`);
    if (seen.has(name)) errors.push(`參數「${name}」重複`);
    seen.add(name);
    const enumMatch = /^enum\((.*)\)$/.exec(rawType.trim());
    const type = (enumMatch ? "enum" : rawType.trim()) as ParameterType;
    if (!["string", "integer", "number", "boolean", "enum"].includes(type)) errors.push(`參數「${name}」的型別「${rawType}」不支援（string／integer／number／boolean／enum(a|b)）`);
    const enumValues = enumMatch ? enumMatch[1].split("|").map((value) => value.trim()).filter(Boolean) : undefined;
    if (type === "enum" && (!enumValues || enumValues.length === 0)) errors.push(`enum 參數「${name}」需要至少一個值`);
    argv.push({ kind: "parameter", name });
    if (["string", "integer", "number", "boolean", "enum"].includes(type)) schema.push(specFor(name, type, enumValues));
  });
  return { argv_template: argv, parameter_schema: schema, errors };
}

/** Coerce one typed-in default to the canonical Product value: JSON ints for
 *  integers and integral numbers, the closed decimal grammar for fractions,
 *  booleans as booleans, strings/enums as strings. Returns an error message
 *  instead of a value when the text cannot be canonical. */
export function canonicalParameterValue(spec: { type: ParameterType; enum_values?: string[] }, raw: string): { value?: string | number | boolean; error?: string } {
  const text = raw.trim();
  switch (spec.type) {
    case "integer":
      return INTEGER_RE.test(text) ? { value: Number(text) } : { error: "必須是整數" };
    case "number":
      if (INTEGER_RE.test(text)) return { value: Number(text) };
      if (CANONICAL_DECIMAL_RE.test(text)) return { value: text };
      return { error: "小數請寫成不含多餘零的形式（例如 0.1，不是 0.10 或 .1）" };
    case "boolean":
      if (text === "true" || text === "false") return { value: text === "true" };
      return { error: "必須是 true 或 false" };
    case "enum":
      return (spec.enum_values ?? []).includes(text) ? { value: text } : { error: `必須是 ${(spec.enum_values ?? []).join("／")} 之一` };
    default:
      return { value: raw };
  }
}
