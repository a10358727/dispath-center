import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type { AgentRunner, Approval, DiffResult, ExperimentItem, LiveServer, Me, Project, ProjectInstance, ProjectVersion, ProjectWorkspace, SessionOptions, SessionSummary, StudioSession } from "./types";

export const keys = {
  me: ["me"] as const,
  projects: ["projects"] as const,
  versions: (name: string) => ["versions", name] as const,
  sessions: (name: string) => ["sessions", name] as const,
  session: (id: string) => ["session", id] as const,
  runners: ["runners"] as const,
  approvals: (status: string) => ["approvals", status] as const,
};

export function useMe() {
  return useQuery({ queryKey: keys.me, queryFn: () => api<Me>("/auth/me"), retry: false, staleTime: 60_000 });
}

/** `/api/v2/projects-matrix` keys `instances` by server name (an object);
 *  older shapes used a list. Normalize to a list with `server` filled in. */
export function normalizeInstances(raw: unknown): ProjectInstance[] {
  if (Array.isArray(raw)) return raw as ProjectInstance[];
  if (raw && typeof raw === "object") {
    return Object.entries(raw as Record<string, unknown>).map(([server, value]) => ({
      server,
      ...((value && typeof value === "object" ? value : {}) as Omit<ProjectInstance, "server">),
    }));
  }
  return [];
}

export function useProjects() {
  return useQuery({
    queryKey: keys.projects,
    queryFn: async () => {
      const data = await api<{ projects: (Omit<Project, "instances"> & { instances?: unknown })[] }>("/api/v2/projects-matrix");
      return (data.projects ?? []).map((project) => ({ ...project, instances: normalizeInstances(project.instances) }));
    },
  });
}

export function useVersions(name: string | undefined) {
  return useQuery({
    queryKey: keys.versions(name ?? ""),
    enabled: Boolean(name),
    queryFn: () => api<ProjectVersion[]>(`/api/v2/legacy-projects/${encodeURIComponent(name ?? "")}/versions`),
  });
}

export function useProjectSessions(name: string | undefined) {
  return useQuery({
    queryKey: keys.sessions(name ?? ""),
    enabled: Boolean(name),
    queryFn: () =>
      api<{ current: SessionSummary | null; recent: SessionSummary[] }>(
        `/api/v2/legacy-projects/${encodeURIComponent(name ?? "")}/agent-sessions`,
      ),
  });
}

export function useSession(id: string | undefined) {
  return useQuery({
    queryKey: keys.session(id ?? ""),
    enabled: Boolean(id),
    queryFn: () => api<StudioSession>(`/api/v2/studio/sessions/${encodeURIComponent(id ?? "")}`),
    refetchInterval: 15_000,
  });
}

export function useRunners() {
  return useQuery({
    queryKey: keys.runners,
    queryFn: async () => {
      const data = await api<{ enabled: boolean; agent_runners: AgentRunner[] }>("/api/v2/agent-runners");
      return { enabled: data.enabled, runners: data.agent_runners ?? [] };
    },
    refetchInterval: 20_000,
  });
}

export function useWorkspace(projectId: string | undefined) {
  return useQuery({
    queryKey: ["workspace", projectId ?? ""],
    enabled: Boolean(projectId),
    queryFn: () => api<ProjectWorkspace>(`/api/v2/projects/${encodeURIComponent(projectId ?? "")}/workspace`),
  });
}

export function useLiveServers() {
  return useQuery({
    queryKey: ["live-servers"],
    queryFn: () => api<LiveServer[]>("/api/v2/servers"),
    refetchInterval: 15_000,
  });
}

export function useServerConfigs() {
  return useQuery({
    queryKey: ["server-configs"],
    queryFn: () => api<{ name: string; tags?: string[] }[]>("/api/v2/server-configs"),
    staleTime: 60_000,
  });
}

export function useExperiments(projectId: string | undefined) {
  return useQuery({
    queryKey: ["experiments", projectId ?? ""],
    enabled: Boolean(projectId),
    queryFn: async () => (await api<{ items: ExperimentItem[] }>(`/api/v2/experiments?project_id=${encodeURIComponent(projectId ?? "")}`)).items ?? [],
    refetchInterval: 10_000,
  });
}

export interface JobRow {
  id: number;
  type?: string;
  project?: string | null;
  status: string;
  server?: string | null;
  created_at?: string;
  started_at?: string | null;
  finished_at?: string | null;
  exit_code?: number | null;
  command?: string;
  stalled_suspect?: boolean;
}

export function useJobs(project: string | undefined, status: string) {
  const query = new URLSearchParams();
  if (project) query.set("project", project);
  if (status) query.set("status", status);
  return useQuery({
    queryKey: ["jobs", project ?? "", status],
    enabled: Boolean(project),
    queryFn: () => api<JobRow[]>(`/api/v2/jobs?${query.toString()}`),
    refetchInterval: 10_000,
  });
}

export function useServerOccupancy() {
  return useQuery({
    queryKey: ["server-occupancy"],
    queryFn: async () => {
      const [running, queued] = await Promise.all([
        api<JobRow[]>("/api/v2/jobs?status=running"),
        api<JobRow[]>("/api/v2/jobs?status=queued"),
      ]);
      const counts: Record<string, { running: number; queued: number }> = {};
      for (const job of running) {
        const key = job.server ?? "?";
        counts[key] = counts[key] ?? { running: 0, queued: 0 };
        counts[key].running += 1;
      }
      for (const job of queued) {
        const key = job.server ?? job.status;
        counts[key] = counts[key] ?? { running: 0, queued: 0 };
        counts[key].queued += 1;
      }
      return { counts, queuedTotal: queued.length };
    },
    refetchInterval: 15_000,
  });
}

export function useIdleSummary() {
  return useQuery({
    queryKey: ["idle-summary"],
    queryFn: () => api<{ server_name: string; gpu_util_p50?: number | null; gpu_util_p95?: number | null; continuous_idle_seconds?: number | null; status?: string }[]>(
      "/api/v2/servers/idle-summary?hours=24",
    ),
    refetchInterval: 60_000,
  });
}

export function useJobLog(jobId: number | null) {
  return useQuery({
    queryKey: ["job-log", jobId ?? 0],
    enabled: jobId != null,
    queryFn: () => api<{ job_id: number; status: string; live: boolean; log_tail: string }>(`/api/v2/jobs/${jobId}/log?lines=80`),
    refetchInterval: 5_000,
  });
}

/** Decide through the generic v2 decisions route. It handles every kind:
 *  typed branches (experiment/plan/bootstrap/dataset/…) directly, everything
 *  else through the compatibility branch — which REQUIRES the
 *  `X-Approval-Payload-Digest` of the card as reviewed, so the detail is
 *  fetched first and its digest forwarded (approve-what-you-saw). */
export function useDecideApprovalV2() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, decision, note }: { id: number; decision: "approve" | "reject"; note?: string }) => {
      const detail = await api<Approval>(`/api/v2/approvals/${id}`);
      const headers: Record<string, string> = { "Idempotency-Key": crypto.randomUUID() };
      if (detail.payload_digest) headers["X-Approval-Payload-Digest"] = detail.payload_digest;
      return api<Record<string, unknown>>(`/api/v2/approvals/${id}/decisions`, {
        method: "POST",
        json: { decision, note: note || undefined },
        headers,
      });
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["approvals"] });
      void client.invalidateQueries({ queryKey: ["experiments"] });
      void client.invalidateQueries({ queryKey: ["sessions"] });
    },
  });
}

export function useCostSummary() {
  return useQuery({
    queryKey: ["cost-summary"],
    queryFn: () => api<{ projects: { project: string; sessions: number; cost_usd: number }[]; total_cost_usd: number }>("/api/v2/studio/cost-summary"),
  });
}

export function useApprovals(status: string, limit = 50) {
  return useQuery({
    queryKey: keys.approvals(status),
    queryFn: async () => (await api<{ items: Approval[] }>(`/api/v2/approvals?status=${status}&limit=${limit}`)).items ?? [],
    refetchInterval: status === "pending" ? 10_000 : false,
  });
}

/** Decide one approval where it appears (DG-STUDIO-UI v1: no page hop).
 *  Uses the reviewed legacy decision routes; the browser session is the actor. */
export function useDecideApproval() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, decision, note }: { id: number; decision: "approve" | "reject"; note?: string }) =>
      api<Record<string, unknown>>(decision === "approve" ? `/approve/${id}` : `/reject/${id}`, {
        method: "POST",
        json: decision === "reject" ? { note: note ?? "" } : undefined,
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["approvals"] });
      void client.invalidateQueries({ queryKey: ["sessions"] });
      void client.invalidateQueries({ queryKey: keys.runners });
    },
  });
}

export function useOpenSessionRequest(project: string) {
  return useMutation({
    mutationFn: (body: { base_version_id: string; runner_id: string; options?: SessionOptions; fork_from_session_id?: string }) =>
      api<{ approval: Approval }>(`/api/v2/studio/projects/${encodeURIComponent(project)}/sessions/open-requests`, {
        method: "POST",
        json: body,
      }),
  });
}

export function useSessionActions(id: string) {
  const client = useQueryClient();
  const base = `/api/v2/studio/sessions/${encodeURIComponent(id)}`;
  const refresh = () => void client.invalidateQueries({ queryKey: keys.session(id) });
  const start = useMutation({ mutationFn: () => api(`${base}/start`, { method: "POST" }), onSuccess: refresh });
  const send = useMutation({
    mutationFn: ({ text, attachments }: { text: string; attachments?: { type: "image"; media_type: string; data_base64: string }[] }) =>
      api(`${base}/messages`, { method: "POST", json: { text, attachments } }),
  });
  const interrupt = useMutation({ mutationFn: () => api(`${base}/interrupt`, { method: "POST" }) });
  const close = useMutation({ mutationFn: () => api(`${base}/close`, { method: "POST" }), onSuccess: refresh });
  const diff = useMutation({ mutationFn: () => api<DiffResult>(`${base}/diff`) });
  const files = useMutation({ mutationFn: () => api<{ files: string[] }>(`${base}/files`) });
  const checkpoint = useMutation({ mutationFn: () => api<{ approval: Approval }>(`${base}/checkpoint-requests`, { method: "POST" }) });
  const configure = useMutation({
    mutationFn: (changes: { model?: string; permission_mode?: string }) => api<{ options: SessionOptions }>(`${base}/configure`, { method: "POST", json: changes }),
    onSuccess: refresh,
  });
  const decide = useMutation({
    mutationFn: ({ requestId, decision, allowPattern }: { requestId: string; decision: "allow" | "deny"; allowPattern?: string }) =>
      api(`${base}/permissions/${encodeURIComponent(requestId)}/decision`, {
        method: "POST",
        json: { decision, allow_pattern: allowPattern ?? null },
      }),
    onSuccess: refresh,
  });
  return { start, send, interrupt, close, diff, decide, configure, files, checkpoint };
}
