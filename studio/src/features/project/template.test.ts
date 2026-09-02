import { describe, expect, it } from "vitest";
import { canonicalParameterValue, compileCommandTemplate } from "./template";

describe("compileCommandTemplate (整頓 U4b)", () => {
  it("compiles literals and typed parameters into the strict spec shape", () => {
    const compiled = compileCommandTemplate("python train.py --lr {lr:number} --bs {bs:integer} --mode {mode:enum(b|a)} --tag {tag} --amp {amp:boolean}");
    expect(compiled.errors).toEqual([]);
    expect(compiled.argv_template).toEqual([
      { kind: "literal", value: "python" },
      { kind: "literal", value: "train.py" },
      { kind: "literal", value: "--lr" },
      { kind: "parameter", name: "lr" },
      { kind: "literal", value: "--bs" },
      { kind: "parameter", name: "bs" },
      { kind: "literal", value: "--mode" },
      { kind: "parameter", name: "mode" },
      { kind: "literal", value: "--tag" },
      { kind: "parameter", name: "tag" },
      { kind: "literal", value: "--amp" },
      { kind: "parameter", name: "amp" },
    ]);
    const byName = Object.fromEntries(compiled.parameter_schema.map((spec) => [spec.name, spec]));
    expect(byName.lr).toMatchObject({ type: "number", required: true, sensitive: false, minimum: -1000000000, maximum: 1000000000 });
    expect(byName.bs).toMatchObject({ type: "integer", minimum: -2147483648, maximum: 2147483647 });
    expect(byName.mode).toMatchObject({ type: "enum", enum_values: ["a", "b"] });
    expect(byName.tag).toMatchObject({ type: "string", min_length: 0, max_length: 4096 });
    expect(byName.amp).toEqual({ name: "amp", type: "boolean", required: true, sensitive: false });
  });

  it("rejects the shapes the backend would reject", () => {
    expect(compileCommandTemplate("").errors).toContain("指令不能是空的");
    expect(compileCommandTemplate("{cmd} train.py").errors.join()).toContain("第一個引數必須是可執行檔");
    expect(compileCommandTemplate("python a.py --lr={lr}").errors.join()).toContain("參數必須是獨立的引數");
    expect(compileCommandTemplate("python a.py {x} {x}").errors.join()).toContain("重複");
    expect(compileCommandTemplate("python a.py {1x}").errors.join()).toContain("識別字");
    expect(compileCommandTemplate("python a.py {x:float}").errors.join()).toContain("不支援");
    expect(compileCommandTemplate("python a.py {m:enum()}").errors.join()).toContain("至少一個值");
  });
});

describe("canonicalParameterValue", () => {
  it("follows the canonical number grammar", () => {
    expect(canonicalParameterValue({ type: "integer" }, " 42 ")).toEqual({ value: 42 });
    expect(canonicalParameterValue({ type: "integer" }, "4.2").error).toBeTruthy();
    expect(canonicalParameterValue({ type: "number" }, "3")).toEqual({ value: 3 });
    expect(canonicalParameterValue({ type: "number" }, "0.1")).toEqual({ value: "0.1" });
    expect(canonicalParameterValue({ type: "number" }, "0.10").error).toBeTruthy();
    expect(canonicalParameterValue({ type: "number" }, ".5").error).toBeTruthy();
    expect(canonicalParameterValue({ type: "boolean" }, "true")).toEqual({ value: true });
    expect(canonicalParameterValue({ type: "boolean" }, "yes").error).toBeTruthy();
    expect(canonicalParameterValue({ type: "enum", enum_values: ["a", "b"] }, "c").error).toBeTruthy();
    expect(canonicalParameterValue({ type: "string" }, "any text")).toEqual({ value: "any text" });
  });
});
