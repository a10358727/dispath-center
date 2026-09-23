import { useState } from "react";
import { ApiError, api } from "@/api/client";
import type { SSHHostIdentity } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

type Observation = {
  algorithm: string;
  fingerprint_sha256: string;
  public_key?: string;
  state?: SSHHostIdentity["state"];
};

function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : "Unknown error";
}

export function HostIdentityPanel({ name, identity, onChanged }: { name: string; identity?: SSHHostIdentity | null; onChanged: () => void }) {
  const [observation, setObservation] = useState<Observation | null>(null);
  const [expected, setExpected] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const call = async (path: string, body?: Record<string, unknown>) => {
    setBusy(true); setError(null);
    try {
      const suffix = path === "observe" || path === "revoke" ? path : `actions/${path}`;
      const result = await api<Observation>(`/api/v2/server-configs/${encodeURIComponent(name)}/host-identity/${suffix}`, { method: "POST", json: body ?? {} });
      if (path === "observe") setObservation(result);
      else { setObservation(null); setExpected(""); onChanged(); }
    } catch (exc) { setError(errorText(exc)); }
    finally { setBusy(false); }
  };

  const action = identity?.state === "mismatch" ? "replace" : identity?.state === "rebind_required" ? "rebind" : "trust";
  const label = identity?.state === "mismatch" ? "Blocked — host identity changed" : identity?.state === "rebind_required" ? "Rebind required" : identity?.verification_method === "tofu" ? "Trusted via TOFU — not independently verified" : identity ? "Independently verified" : "Host identity not trusted";
  const tone = identity?.state === "mismatch" ? "bad" : identity?.state === "rebind_required" || !identity ? "warn" : "ok";

  return <div className="space-y-2 rounded border border-slate-200 bg-slate-50 p-2">
    <div className="flex flex-wrap items-center gap-2"><strong>SSH host identity</strong><Badge tone={tone}>{label}</Badge></div>
    {identity?.fingerprint_sha256 ? <code className="block break-all text-xs">{identity.algorithm} {identity.fingerprint_sha256}</code> : null}
    <Button disabled={busy} onClick={() => void call("observe")}>{busy ? "Checking…" : "Observe current host key"}</Button>
    {observation ? <div className="space-y-2 rounded bg-white p-2">
      <p className="text-xs">Observed at <strong>{identity?.host ?? "configured host"}:{identity?.port ?? 22}</strong></p>
      <code className="block break-all text-xs">{observation.algorithm} {observation.fingerprint_sha256}</code>
      <label className="block text-xs"><span className="block">Provider fingerprint (preferred)</span><input aria-label="Provider SHA256 fingerprint" className="w-full rounded border px-2 py-1" placeholder="SHA256:…" value={expected} onChange={(event) => setExpected(event.target.value)} /></label>
      <label className="block text-xs"><span className="block">Reason (required for replace/rebind)</span><input aria-label="Host identity reason" className="w-full rounded border px-2 py-1" value={reason} onChange={(event) => setReason(event.target.value)} /></label>
      <div className="flex flex-wrap gap-2">
        <Button variant="primary" disabled={busy || !expected.trim() || (action !== "trust" && !reason.trim())} onClick={() => void call(action, { verification_method: "oob", expected_fingerprint_sha256: expected.trim(), reason: reason.trim() || undefined })}>Verify provider fingerprint &amp; {action}</Button>
        <Button disabled={busy || (action !== "trust" && !reason.trim())} onClick={() => void call(action, { verification_method: "tofu", acknowledge_tofu: true, reason: reason.trim() || undefined })}>Explicitly accept observed key (TOFU)</Button>
      </div>
      <p className="text-xs text-amber-800">Use TOFU only when the provider has no independent fingerprint. It will be recorded as not independently verified.</p>
    </div> : null}
    {identity && identity.state !== "revoked" ? <Button variant="ghost" disabled={busy || !reason.trim()} onClick={() => void call("revoke", { reason: reason.trim() })}>Revoke identity</Button> : null}
    {error ? <p role="alert" className="text-xs text-rose-800">{error}</p> : null}
  </div>;
}
