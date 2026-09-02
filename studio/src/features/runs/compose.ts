/** Pure helpers behind the single run form (整頓 U6). One form covers a
 *  single Run and a matrix: every parameter takes one value or a list; any
 *  list makes the request an experiment (`experiment_create_v2`), otherwise
 *  it is one `execution_plan_v2`. Endpoints and digest rituals are unchanged. */

import type { ProjectWorkspace, TemplateParameter } from "@/api/types";
import { expandedRunCount, parseAxisValues, type AxisValue, type MatrixAxis } from "@/features/experiments/matrix";

export const MAX_EXPERIMENT_RUNS = 32;

export interface SplitParameters {
  fixed: Record<string, AxisValue>;
  axes: MatrixAxis[];
  errors: Record<string, string>;
}

/** Text per parameter → fixed values (one value) and axes (several values). */
export function splitParameters(values: Record<string, string>, parameters: TemplateParameter[]): SplitParameters {
  const fixed: Record<string, AxisValue> = {};
  const axes: MatrixAxis[] = [];
  const errors: Record<string, string> = {};
  for (const parameter of parameters) {
    const raw = (values[parameter.name] ?? "").trim();
    if (!raw) continue;
    const parsed = parseAxisValues(raw, parameter.type);
    if (parsed.error) {
      errors[parameter.name] = parsed.error;
      continue;
    }
    if (parsed.values.length === 1) fixed[parameter.name] = parsed.values[0];
    else axes.push({ name: parameter.name, values: parsed.values });
  }
  return { fixed, axes, errors };
}

/** Defaults head when the project has one, else the exact template head —
 *  the run resolver accepts either (app/execution_plan_v2_store.py). */
export function buildTemplateSelection(workspace: ProjectWorkspace | undefined) {
  const defaultsRevision = workspace?.defaults?.revision_id;
  if (defaultsRevision) return { kind: "project_defaults" as const, project_defaults_revision_id: defaultsRevision };
  const templateId = workspace?.run_template?.id;
  if (templateId) return { kind: "run_profile_revision" as const, run_profile_id: templateId };
  return null;
}

/** Without defaults every required parameter must be fixed or an axis. */
export function missingRequiredParameters(split: SplitParameters, parameters: TemplateParameter[], hasDefaults: boolean): string[] {
  if (hasDefaults) return [];
  const covered = new Set([...Object.keys(split.fixed), ...split.axes.map((axis) => axis.name)]);
  return parameters.filter((parameter) => parameter.required !== false && !covered.has(parameter.name)).map((parameter) => parameter.name);
}

export function composeError(split: SplitParameters, parameters: TemplateParameter[], hasDefaults: boolean, servers: string[], versionId: string): string | null {
  if (!versionId) return "沒有可用的正式版本";
  if (Object.keys(split.errors).length > 0) return "有參數的值格式不對";
  const missing = missingRequiredParameters(split, parameters, hasDefaults);
  if (missing.length > 0) return `沒有預設參數，請填齊：${missing.join("、")}`;
  if (servers.length === 0) return "至少選一台執行機器";
  const runs = split.axes.length > 0 ? expandedRunCount(split.axes) : 1;
  if (runs > MAX_EXPERIMENT_RUNS) return `展開 ${runs} 個 run，超過上限 ${MAX_EXPERIMENT_RUNS}`;
  if (split.axes.length === 0 && servers.length > 1) return "單一 Run 只能選一台執行機器（給多個參數值就會展開成實驗）";
  return null;
}

export function runCount(split: SplitParameters): number {
  return split.axes.length > 0 ? expandedRunCount(split.axes) : 1;
}

export function buildExperimentBody(input: { versionId: string; workspace: ProjectWorkspace | undefined; split: SplitParameters; servers: string[] }) {
  return {
    project_version_id: input.versionId,
    template_selection: buildTemplateSelection(input.workspace),
    dataset_selection: { kind: "none" },
    matrix: { axes: input.split.axes },
    parameter_overrides: input.split.fixed,
    guard: { total_runs: runCount(input.split), target_servers: input.servers, est_gpu_hours: null, est_storage: null },
  };
}

export function buildRunBody(input: { versionId: string; workspace: ProjectWorkspace | undefined; split: SplitParameters; targetRevisionId: string }) {
  return {
    project_version_id: input.versionId,
    template_selection: buildTemplateSelection(input.workspace),
    parameter_overrides: input.split.fixed,
    dataset_selection: { kind: "none" },
    target_selection: { kind: "server_config_revision", server_config_revision_id: input.targetRevisionId },
  };
}
