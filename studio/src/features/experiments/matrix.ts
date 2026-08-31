/** Mirrors the server's `expand_matrix` semantics (app/experiment_v2.py):
 *  axes iterated in sorted-by-name order, each axis's declared value order
 *  preserved, cartesian product — so preview labels line up with
 *  `plan_digests[]` expansion order. */

export type AxisValue = string | number | boolean;

export interface MatrixAxis {
  name: string;
  values: AxisValue[];
}

export function expandedRunCount(axes: MatrixAxis[]): number {
  if (axes.length === 0) return 0;
  return axes.reduce((product, axis) => product * axis.values.length, 1);
}

export function expandCombinations(axes: MatrixAxis[]): Record<string, AxisValue>[] {
  const ordered = [...axes].sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  let combos: Record<string, AxisValue>[] = [{}];
  for (const axis of ordered) {
    const next: Record<string, AxisValue>[] = [];
    for (const combo of combos) {
      for (const value of axis.values) {
        next.push({ ...combo, [axis.name]: value });
      }
    }
    combos = next;
  }
  return combos;
}

/** Parse a comma/newline separated value list according to the template
 *  parameter type. `number` stays a canonical decimal *string* (the server
 *  rejects JSON floats); `integer` becomes int; `boolean` true/false. */
export function parseAxisValues(raw: string, type: string | undefined): { values: AxisValue[]; error: string | null } {
  const parts = raw
    .split(/[\n,]/)
    .map((part) => part.trim())
    .filter(Boolean);
  const seen = new Set<string>();
  const values: AxisValue[] = [];
  for (const part of parts) {
    if (seen.has(part)) continue;
    seen.add(part);
    if (type === "integer") {
      if (!/^-?\d+$/.test(part)) return { values: [], error: `「${part}」不是整數` };
      values.push(Number.parseInt(part, 10));
    } else if (type === "boolean") {
      if (part !== "true" && part !== "false") return { values: [], error: `「${part}」必須是 true/false` };
      values.push(part === "true");
    } else if (type === "number") {
      if (!/^-?\d+(\.\d+)?$/.test(part)) return { values: [], error: `「${part}」不是十進位數字` };
      values.push(part); // canonical decimal string, never a JSON float
    } else {
      values.push(part);
    }
  }
  if (values.length === 0) return { values: [], error: "至少要一個值" };
  return { values, error: null };
}
