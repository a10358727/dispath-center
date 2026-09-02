import { describe, expect, it } from "vitest";
import { buildExperimentBody, buildRunBody, buildTemplateSelection, composeError, missingRequiredParameters, runCount, splitParameters } from "./compose";
import type { ProjectWorkspace, TemplateParameter } from "@/api/types";

const parameters: TemplateParameter[] = [
  { name: "lr", type: "number", required: true },
  { name: "epochs", type: "integer", required: true },
  { name: "tag", type: "string", required: true },
];

describe("splitParameters (整頓 U6)", () => {
  it("keeps one value fixed and turns lists into axes", () => {
    const split = splitParameters({ lr: "0.1, 0.01", epochs: "3", tag: "" }, parameters);
    expect(split.fixed).toEqual({ epochs: 3 });
    expect(split.axes).toEqual([{ name: "lr", values: ["0.1", "0.01"] }]);
    expect(split.errors).toEqual({});
    expect(runCount(split)).toBe(2);
  });

  it("reports type errors per parameter", () => {
    const split = splitParameters({ epochs: "three" }, parameters);
    expect(split.errors.epochs).toContain("不是整數");
  });
});

describe("template selection and composition", () => {
  const withDefaults: ProjectWorkspace = { defaults: { revision_id: "def-1" }, run_template: { id: "rp-1" } };
  const withoutDefaults: ProjectWorkspace = { defaults: null, run_template: { id: "rp-1" } };

  it("prefers the defaults head and falls back to the template head", () => {
    expect(buildTemplateSelection(withDefaults)).toEqual({ kind: "project_defaults", project_defaults_revision_id: "def-1" });
    expect(buildTemplateSelection(withoutDefaults)).toEqual({ kind: "run_profile_revision", run_profile_id: "rp-1" });
    expect(buildTemplateSelection({})).toBeNull();
  });

  it("requires every parameter when there are no defaults", () => {
    const split = splitParameters({ lr: "0.1" }, parameters);
    expect(missingRequiredParameters(split, parameters, false)).toEqual(["epochs", "tag"]);
    expect(missingRequiredParameters(split, parameters, true)).toEqual([]);
    expect(composeError(split, parameters, false, ["a"], "v1")).toContain("epochs、tag");
  });

  it("routes lists to an experiment body and single values to a run body", () => {
    const matrix = splitParameters({ lr: "0.1, 0.01", epochs: "3, 5", tag: "x" }, parameters);
    expect(composeError(matrix, parameters, true, ["a", "b"], "v1")).toBeNull();
    const experiment = buildExperimentBody({ versionId: "v1", workspace: withDefaults, split: matrix, servers: ["a", "b"] });
    expect(experiment.matrix.axes).toHaveLength(2);
    expect(experiment.parameter_overrides).toEqual({ tag: "x" });
    expect(experiment.guard).toMatchObject({ total_runs: 4, target_servers: ["a", "b"] });

    const single = splitParameters({ lr: "0.1", epochs: "3", tag: "x" }, parameters);
    expect(composeError(single, parameters, true, ["a"], "v1")).toBeNull();
    expect(composeError(single, parameters, true, ["a", "b"], "v1")).toContain("單一 Run 只能選一台");
    const run = buildRunBody({ versionId: "v1", workspace: withoutDefaults, split: single, targetRevisionId: "rev-a" });
    expect(run.template_selection).toEqual({ kind: "run_profile_revision", run_profile_id: "rp-1" });
    expect(run.parameter_overrides).toEqual({ lr: "0.1", epochs: 3, tag: "x" });
    expect(run.target_selection).toEqual({ kind: "server_config_revision", server_config_revision_id: "rev-a" });
  });

  it("caps the expansion at 32 runs", () => {
    const big = splitParameters({ lr: "1,2,3,4,5,6", epochs: "1,2,3,4,5,6", tag: "x" }, parameters);
    expect(composeError(big, parameters, true, ["a"], "v1")).toContain("超過上限 32");
  });
});
