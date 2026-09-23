import { useMemo, useState } from "react";
import { ApiError, api } from "@/api/client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { HostIdentityPanel } from "./HostIdentityPanel";

type ComputeKind = "rental" | "owned" | "fpga";
interface Draft { kind: ComputeKind; name: string; host: string; port: string; user: string; key: string; tags: string; }
interface ProbeResult { ok: boolean; results?: Record<string, unknown>; errors?: string[]; }
interface PreflightResult { status?: string; filesystem_type?: string | null; }
const kinds = [
  { value: "rental" as const, title: "Rental GPU", description: "A rented SSH host. Give every rental a unique Compute name.", tags: ["rental", "gpu"], gpu: true },
  { value: "owned" as const, title: "My Server", description: "A server you operate and reach over SSH.", tags: ["owned"], gpu: false },
  { value: "fpga" as const, title: "FPGA Host", description: "An SSH host classified for hardware work.", tags: ["fpga"], gpu: false },
];
const initial: Draft = { kind: "rental", name: "", host: "", port: "22", user: "", key: "", tags: "" };
const errorText = (error: unknown) => error instanceof ApiError ? error.message : error instanceof Error ? error.message : "Unknown error";

export function AddComputeWizard({ existingNames, onAdded, onCancel }: { existingNames: string[]; onAdded: () => void; onCancel: () => void }) {
  const [step, setStep] = useState(1), [draft, setDraft] = useState(initial);
  const [created, setCreated] = useState(false), [trusted, setTrusted] = useState(false);
  const [busy, setBusy] = useState(false), [error, setError] = useState<string | null>(null);
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const [completed, setCompleted] = useState<{ state: "eligible" | "blocked" | "unknown" | "error"; detail: string } | null>(null);
  const kind = kinds.find((item) => item.value === draft.kind) ?? kinds[0];
  const payload = useMemo(() => ({ name: draft.name.trim(), host: draft.host.trim(), port: Number.parseInt(draft.port, 10), user: draft.user.trim(), key: draft.key.trim(), tags: [...new Set([...kind.tags, ...draft.tags.split(",").map((tag) => tag.trim()).filter(Boolean)])], gpu: kind.gpu, enabled: true }), [draft, kind]);
  const valid = Boolean(payload.name && payload.host && payload.user && payload.key && payload.port > 0 && payload.port <= 65535 && !existingNames.includes(payload.name));

  async function persist() { setBusy(true); setError(null); try { await api("/api/v2/server-configs", { method: "POST", json: payload }); setCreated(true); onAdded(); } catch (exc) { setError(errorText(exc)); } finally { setBusy(false); } }
  async function verify() {
    setBusy(true); setError(null); setProbe(null);
    try {
      const result = await api<ProbeResult>("/api/v2/server-configs/test-ssh", { method: "POST", json: payload }); setProbe(result);
      if (!result.ok) { setError(`Compute remains added and trusted. ${(result.errors ?? ["Connection test failed."]).join(" ")}`); return; }
      try {
        const preflight = await api<PreflightResult>(`/api/v2/server-configs/${encodeURIComponent(payload.name)}/attempt-preflight`, { method: "POST", json: {} });
        if (preflight.status === "eligible") setCompleted({ state: "eligible", detail: `Local attempt filesystem detected${preflight.filesystem_type ? ` (${preflight.filesystem_type})` : ""}.` });
        else if (preflight.status === "ineligible_non_local_fs") setCompleted({ state: "blocked", detail: "The attempt filesystem is not local." });
        else setCompleted({ state: "unknown", detail: "The filesystem preflight returned no recognized result." });
      } catch (exc) { setCompleted({ state: "error", detail: `Compute remains added and trusted, but preflight failed: ${errorText(exc)}` }); }
    } catch (exc) { setError(`Compute remains added and trusted. Connection test failed: ${errorText(exc)}`); }
    finally { setBusy(false); }
  }

  if (completed) return <section aria-label="Add Compute" className="space-y-3 rounded border border-emerald-200 bg-emerald-50 p-4"><h3 className="font-semibold">Compute added: {payload.name}</h3><div role="status"><Badge tone={completed.state === "eligible" ? "ok" : completed.state === "unknown" ? "warn" : "bad"}>{completed.state === "eligible" ? "Preflight eligible" : completed.state === "blocked" ? "Preflight blocked" : completed.state === "error" ? "Preflight error" : "Preflight unknown"}</Badge> <span>{completed.detail}</span></div><Button variant="primary" onClick={onCancel}>Done</Button></section>;

  const fields: Array<[keyof Draft, string]> = [["name", "Unique Compute name"], ["host", "Host"], ["port", "SSH port"], ["user", "SSH user"], ["key", "Private-key path reference"], ["tags", "Additional tags (comma-separated)"]];
  return <section aria-label="Add Compute" className="space-y-4 rounded border border-sky-200 bg-sky-50 p-4">
    <div className="flex justify-between"><div><h3 className="font-semibold">Add Compute</h3><p className="text-xs">Step {step} of 4</p></div><Button variant="ghost" onClick={onCancel}>Cancel</Button></div>
    {step === 1 ? <div className="grid gap-2 md:grid-cols-3">{kinds.map((item) => <button key={item.value} type="button" aria-pressed={draft.kind === item.value} className="rounded border bg-white p-3 text-left" onClick={() => setDraft({ ...draft, kind: item.value })}><strong>{item.title}</strong><span className="block text-xs">{item.description}</span></button>)}</div> : null}
    {step === 2 ? <div className="grid gap-3 md:grid-cols-2">{fields.map(([field, label]) => <label key={field} className="text-sm"><span className="block">{label}</span><input aria-label={label} className="w-full rounded border px-2 py-1" type={field === "port" ? "number" : "text"} value={draft[field]} onChange={(event) => setDraft({ ...draft, [field]: event.target.value })} /></label>)}<p className="col-span-full text-xs">Use a private-key path reference. Never paste key contents.</p></div> : null}
    {step === 3 ? <div className="space-y-3"><p>Persist the logical Compute first, then verify and pin its host identity. No SSH command runs before trust.</p>{!created ? <Button variant="primary" disabled={busy} onClick={() => void persist()}>{busy ? "Adding Compute…" : "Add Compute configuration"}</Button> : <><Badge tone="ok">Compute configuration added</Badge>{trusted ? <Badge tone="ok">Host identity trusted</Badge> : <HostIdentityPanel name={payload.name} identity={null} onChanged={() => setTrusted(true)} />}</>}</div> : null}
    {step === 4 ? <div className="space-y-3"><p>{payload.user}@{payload.host}:{payload.port}</p><p className="text-xs">The host identity is pinned. A successful fixed SSH probe is followed by filesystem preflight.</p>{probe?.ok ? <Badge tone="ok">Connection succeeded</Badge> : null}<Button variant="primary" disabled={busy} onClick={() => void verify()}>{busy ? "Verifying…" : "Test trusted connection & run preflight"}</Button></div> : null}
    {error ? <div role="alert" className="rounded bg-rose-100 p-2 text-sm">{error}</div> : null}
    <div className="flex justify-between"><Button variant="ghost" disabled={step === 1 || busy || created} onClick={() => setStep(step - 1)}>Back</Button>{step < 4 ? <Button variant="primary" disabled={(step === 2 && !valid) || (step === 3 && !trusted)} onClick={() => setStep(step + 1)}>Continue</Button> : null}</div>
  </section>;
}
