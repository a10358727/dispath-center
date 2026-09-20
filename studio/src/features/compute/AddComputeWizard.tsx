import { useMemo, useState } from "react";
import { ApiError, api } from "@/api/client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

type ComputeKind = "rental" | "owned" | "fpga";

interface Draft {
  kind: ComputeKind;
  name: string;
  host: string;
  port: string;
  user: string;
  key: string;
  tags: string;
}

interface ProbeResult {
  ok: boolean;
  results?: Record<string, unknown>;
  warnings?: string[];
  errors?: string[];
}

interface PreflightResult {
  ok?: boolean;
  status?: string;
  filesystem_type?: string | null;
}

const kindOptions: Array<{ value: ComputeKind; title: string; description: string; tags: string[]; gpu: boolean }> = [
  { value: "rental", title: "Rental GPU", description: "A rented SSH host. Give every rental a unique Compute name; disable it when the rental ends.", tags: ["rental", "gpu"], gpu: true },
  { value: "owned", title: "My Server", description: "A server you operate and reach over SSH.", tags: ["owned"], gpu: false },
  { value: "fpga", title: "FPGA Host", description: "An SSH host classified for hardware work. This does not declare or detect a device.", tags: ["fpga"], gpu: false },
];

const initialDraft: Draft = { kind: "rental", name: "", host: "", port: "22", user: "", key: "", tags: "" };

function parseTags(value: string): string[] {
  return [...new Set(value.split(",").map((tag) => tag.trim()).filter(Boolean))];
}

function errorText(error: unknown): string {
  if (error instanceof ApiError && error.details) {
    const detail = typeof error.details === "string" ? error.details : JSON.stringify(error.details);
    return `${error.message} (${detail})`;
  }
  return error instanceof Error ? error.message : "Unknown error";
}

export function AddComputeWizard({ existingNames, onAdded, onCancel }: { existingNames: string[]; onAdded: () => void; onCancel: () => void }) {
  const [step, setStep] = useState(1);
  const [draft, setDraft] = useState<Draft>(initialDraft);
  const [testedPayloadKey, setTestedPayloadKey] = useState<string | null>(null);
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const [testing, setTesting] = useState(false);
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [completed, setCompleted] = useState<{ preflight: "eligible" | "ineligible" | "unknown" | "error"; detail: string } | null>(null);

  const kind = kindOptions.find((option) => option.value === draft.kind) ?? kindOptions[0];
  const payload = useMemo(() => ({
    name: draft.name.trim(), host: draft.host.trim(), port: Number.parseInt(draft.port, 10), user: draft.user.trim(), key: draft.key.trim(),
    tags: [...new Set([...kind.tags, ...parseTags(draft.tags)])], gpu: kind.gpu, enabled: true,
  }), [draft, kind]);
  const payloadKey = JSON.stringify(payload);
  const currentTestPassed = Boolean(probe?.ok && testedPayloadKey === payloadKey);
  const nameExists = existingNames.includes(payload.name);
  const portValid = Number.isInteger(payload.port) && payload.port >= 1 && payload.port <= 65535;
  const connectionComplete = Boolean(payload.name && payload.host && payload.user && payload.key && portValid && !nameExists);

  const update = (field: keyof Draft, value: string) => {
    setDraft((current) => ({ ...current, [field]: value }));
    setError(null);
  };

  const testConnection = async () => {
    setTesting(true); setError(null); setProbe(null);
    try {
      const result = await api<ProbeResult>("/api/v2/server-configs/test-ssh", { method: "POST", json: payload });
      setProbe(result); setTestedPayloadKey(payloadKey);
      if (!result.ok) setError((result.errors ?? ["The SSH probe did not succeed."]).join(" "));
    } catch (exc) {
      setTestedPayloadKey(payloadKey); setError(errorText(exc));
    } finally { setTesting(false); }
  };

  const addCompute = async () => {
    if (!currentTestPassed) return;
    setAdding(true); setError(null);
    try {
      await api("/api/v2/server-configs", { method: "POST", json: payload });
      onAdded();
      try {
        const result = await api<PreflightResult>(`/api/v2/server-configs/${encodeURIComponent(payload.name)}/attempt-preflight`, { method: "POST", json: {} });
        if (result.status === "eligible") {
          setCompleted({ preflight: "eligible", detail: `Local attempt filesystem detected${result.filesystem_type ? ` (${result.filesystem_type})` : ""}.` });
        } else if (result.status === "ineligible_non_local_fs") {
          setCompleted({ preflight: "ineligible", detail: "The attempt filesystem is not local, so this Compute is not eligible for governed execution." });
        } else {
          setCompleted({ preflight: "unknown", detail: "The filesystem preflight returned no recognized eligibility result." });
        }
      } catch (exc) {
        setCompleted({ preflight: "error", detail: `Compute was added, but its stored filesystem preflight could not be completed: ${errorText(exc)}` });
      }
    } catch (exc) { setError(errorText(exc)); }
    finally { setAdding(false); }
  };

  if (completed) return (
    <section aria-label="Add Compute" className="space-y-3 rounded border border-emerald-200 bg-emerald-50 p-4">
      <h3 className="font-semibold">Compute added: {payload.name}</h3>
      <div role="status" className="flex items-center gap-2 text-sm">
        <Badge tone={completed.preflight === "eligible" ? "ok" : completed.preflight === "ineligible" || completed.preflight === "error" ? "bad" : "warn"}>
          {completed.preflight === "eligible" ? "Preflight eligible" : completed.preflight === "ineligible" ? "Preflight blocked" : completed.preflight === "error" ? "Preflight error" : "Preflight unknown"}
        </Badge>
        <span>{completed.detail}</span>
      </div>
      <p className="text-xs text-slate-600">Connection testing and filesystem preflight report only the checks performed by the server. They do not verify SSH host identity or declare every runtime dependency ready.</p>
      <Button variant="primary" onClick={onCancel}>Done</Button>
    </section>
  );

  return (
    <section aria-label="Add Compute" className="space-y-4 rounded border border-sky-200 bg-sky-50 p-4">
      <div className="flex items-center justify-between"><div><h3 className="font-semibold">Add Compute</h3><p className="text-xs text-slate-600">Step {step} of 4</p></div><Button variant="ghost" onClick={onCancel}>Cancel</Button></div>
      {step === 1 ? <div className="grid gap-2 md:grid-cols-3">{kindOptions.map((option) => <button key={option.value} type="button" aria-pressed={draft.kind === option.value} className={`rounded border p-3 text-left ${draft.kind === option.value ? "border-sky-600 bg-white ring-1 ring-sky-600" : "border-slate-200 bg-white"}`} onClick={() => update("kind", option.value)}><span className="block font-medium">{option.title}</span><span className="mt-1 block text-xs text-slate-600">{option.description}</span></button>)}</div> : null}
      {step === 2 ? <div className="grid gap-3 md:grid-cols-2">
        {([ ["name", "Unique Compute name"], ["host", "Host"], ["port", "SSH port"], ["user", "SSH user"], ["key", "Private-key path reference"], ["tags", "Additional tags (comma-separated)"] ] as Array<[keyof Draft, string]>).map(([field, label]) => <label key={field} className="text-sm"><span className="block text-slate-700">{label}</span><input className="w-full rounded border border-slate-300 bg-white px-2 py-1.5" type={field === "port" ? "number" : "text"} min={field === "port" ? 1 : undefined} max={field === "port" ? 65535 : undefined} autoComplete="off" value={draft[field]} onChange={(event) => update(field, event.target.value)} /></label>)}
        <p className="md:col-span-2 text-xs text-slate-600">Enter a server-side key reference such as <code>~/.ssh/id_ed25519</code>. Never paste private-key contents. The reference is kept as the existing Server credential field.</p>
        {nameExists ? <p role="alert" className="md:col-span-2 text-sm text-rose-700">That Compute name already exists. Use a unique name, including for a new rental.</p> : null}
        {!portValid ? <p role="alert" className="md:col-span-2 text-sm text-rose-700">SSH port must be between 1 and 65535.</p> : null}
      </div> : null}
      {step === 3 ? <div className="space-y-3">
        <p className="text-sm">Test the exact host, custom port, user, and credential reference before adding it. This transient probe does not create a Compute.</p>
        {testedPayloadKey && testedPayloadKey !== payloadKey ? <div role="status" className="rounded bg-amber-100 p-2 text-sm text-amber-900">Connection details changed. Test this configuration again.</div> : null}
        {testing ? <div role="status" className="text-sm">Testing SSH connection…</div> : null}
        {probe && testedPayloadKey === payloadKey ? <div className={`rounded p-3 text-sm ${probe.ok ? "bg-emerald-100 text-emerald-900" : "bg-rose-100 text-rose-900"}`}><strong>{probe.ok ? "Connection succeeded" : "Connection failed"}</strong>{probe.ok ? <ul className="mt-2 list-disc pl-5"><li>Hostname: {String(probe.results?.hostname || "not reported")}</li><li>User: {String(probe.results?.whoami || "not reported")}</li><li>tmux: {probe.results?.tmux ? String(probe.results.tmux) : "not detected"}</li><li>GPU: {probe.results?.gpu ? "reported by nvidia-smi" : "not detected"}</li></ul> : null}{probe.warnings?.length ? <p className="mt-2">Warnings: {probe.warnings.join(" ")}</p> : null}</div> : null}
        <Button variant="primary" disabled={testing} onClick={() => void testConnection()}>{testing ? "Testing…" : "Test Connection"}</Button>
        <p className="text-xs text-slate-600">This check uses the existing fixed SSH probe. It does not establish SSH host-key trust or claim full runtime readiness.</p>
      </div> : null}
      {step === 4 ? <div className="space-y-3 text-sm"><div className="rounded border border-slate-200 bg-white p-3"><dl className="grid grid-cols-[9rem_1fr] gap-1"><dt>Classification</dt><dd>{kind.title}</dd><dt>Name</dt><dd>{payload.name}</dd><dt>Connection</dt><dd>{payload.user}@{payload.host}:{payload.port}</dd><dt>Credential reference</dt><dd>Configured ({payload.key.split("/").pop() || "reference"})</dd><dt>Tags</dt><dd>{payload.tags.join(", ") || "None"}</dd><dt>Connection test</dt><dd><Badge tone={currentTestPassed ? "ok" : "warn"}>{currentTestPassed ? "Passed for this configuration" : "Test required"}</Badge></dd></dl></div><p className="text-xs text-slate-600">Add uses the existing ServerConfig publication and audit path. A stored filesystem preflight runs afterward; a preflight error will not roll back the added Compute.</p><Button variant="primary" disabled={adding || !currentTestPassed} onClick={() => void addCompute()}>{adding ? "Adding Compute…" : "Add Compute"}</Button></div> : null}
      {error ? <div role="alert" className="rounded bg-rose-100 p-2 text-sm text-rose-900">{error} Check the host, port, network reachability, SSH user, and credential reference, then retry.</div> : null}
      <div className="flex justify-between"><Button variant="ghost" disabled={step === 1 || testing || adding} onClick={() => setStep((value) => value - 1)}>Back</Button>{step < 4 ? <Button variant="primary" disabled={(step === 2 && !connectionComplete) || (step === 3 && !currentTestPassed)} onClick={() => setStep((value) => value + 1)}>Continue</Button> : null}</div>
    </section>
  );
}
