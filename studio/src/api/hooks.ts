import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type { AgentRunner, Approval, DiffResult, Me, Project, ProjectVersion, SessionSummary, StudioSession, SessionOptions } from "./types";

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

export function useProjects() {
  return useQuery({
    queryKey: keys.projects,
    queryFn: async () => (await api<{ projects: Project[] }>("/api/v2/projects-matrix")).projects ?? [],
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
    mutationFn: (body: { base_version_id: string; runner_id: string; options?: SessionOptions }) =>
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
  const send = useMutation({ mutationFn: (text: string) => api(`${base}/messages`, { method: "POST", json: { text } }) });
  const interrupt = useMutation({ mutationFn: () => api(`${base}/interrupt`, { method: "POST" }) });
  const close = useMutation({ mutationFn: () => api(`${base}/close`, { method: "POST" }), onSuccess: refresh });
  const diff = useMutation({ mutationFn: () => api<DiffResult>(`${base}/diff`) });
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
  return { start, send, interrupt, close, diff, decide, configure };
}
