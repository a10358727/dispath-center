import { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/api/client";
import {
  useHardwareImages,
  useHardwareReceipts,
  useLiveServers,
  useProjects,
  useRunTemplates,
  useWorkspace,
} from "@/api/hooks";
import type { Approval, HardwareImage, HardwareReceipt, RunTemplateHead } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardTitle } from "@/components/ui/card";
import { ApprovalCard } from "@/features/approvals/ApprovalCard";
import { formatTime } from "@/lib";

//: DG-HARDWARE-EXECUTION v1 P4 — the Studio Hardware panel: a projection of
//: the registry (images, receipts) plus a composer that files a
//: `hardware_action_v2` card. The card is always two-step (never in the
//: 確認並執行 closed list); the browser proposes, the server decides.

export const ACTION_CLASS_LABELS: Record<string, string> = {
  program: "燒錄",
  power: "電源",
  hil_test: "實機測試",
};
const PHYSICAL_CLASSES = new Set(Object.keys(ACTION_CLASS_LABELS));
const POWER_SEQUENCES: { value: "off_on" | "reset"; label: string }[] = [
  { value: "off_on", label: "斷電再上電" },
  { value: "reset", label: "重置" },
];

export function shortDigest(digest: string | null | undefined): string {
  return digest ? digest.slice(0, 12) : "—";
}

export function physicalTemplates(items: RunTemplateHead[] | undefined): RunTemplateHead[] {
  return (items ?? []).filter((item) => item.status === "approved" && PHYSICAL_CLASSES.has(item.head_spec?.action_class ?? "compute"));
}

/** Devices the chosen server declares, with the latest observed presence. */
export function devicesOf(servers: { name: string; devices?: Record<string, string> | null }[] | undefined, server: string): { id: string; presence: string }[] {
  const row = (servers ?? []).find((item) => item.name === server);
  return Object.entries(row?.devices ?? {}).map(([id, presence]) => ({ id, presence }));
}

function presenceTone(presence: string): "ok" | "bad" | "neutral" {
  if (presence === "present") return "ok";
  if (presence === "absent") return "bad";
  return "neutral";
}

function ImageRow({ image, projectId, onChanged }: { image: HardwareImage; projectId: string; onChanged: () => void }) {
  const mark = useMutation({
    mutationFn: () => api<{ image: HardwareImage; marked_now: boolean }>(`/api/v2/projects/${projectId}/hardware-images/${image.id}/known-good`, { method: "POST" }),
    onSuccess: onChanged,
  });
  const error = mark.error instanceof ApiError && mark.error.status === 403 ? "只有平台管理員可以直接標記；或在實機測試核准時勾選。" : mark.error ? (mark.error as Error).message : null;
  return (
    <div className="flex flex-wrap items-center gap-2 rounded border border-slate-200 px-2 py-1 text-xs">
      <span className="font-mono">{shortDigest(image.sha256)}</span>
      <Badge tone="neutral">{image.kind}</Badge>
      <span className="text-slate-500">{image.output_name} · {Math.round(image.size_bytes / 1024)} KiB · {formatTime(image.registered_at)}</span>
      {image.known_good_marked_at ? (
        <Badge tone="ok">known-good（{image.known_good_source === "direct" ? "管理員標記" : "實機測試通過"}）</Badge>
      ) : (
        <Button className="px-2 py-0.5 text-xs" disabled={mark.isPending} onClick={() => mark.mutate()}>
          標記 known-good
        </Button>
      )}
      {error ? <span className="text-rose-700">{error}</span> : null}
    </div>
  );
}

function ReceiptRow({ receipt }: { receipt: HardwareReceipt }) {
  const fields = receipt.receipt_json ? (JSON.parse(receipt.receipt_json) as Record<string, unknown>) : null;
  const tone = receipt.status === "collected" ? "ok" : receipt.status === "missing" ? "neutral" : "bad";
  return (
    <div className="flex flex-wrap items-center gap-2 rounded border border-slate-200 px-2 py-1 text-xs">
      <span>Job #{receipt.job_id}</span>
      <Badge tone="neutral">{ACTION_CLASS_LABELS[receipt.action_class] ?? receipt.action_class}</Badge>
      <Badge tone={tone}>{receipt.status === "missing" ? "無收據（未知）" : receipt.status}</Badge>
      {fields ? (
        <span className="text-slate-500">
          {String(fields.device_id)} · {String(fields.tool)} · verify={String(fields.verify)} · exit={String(fields.exit_code)}
          {fields.image_sha256 ? ` · 映像 ${shortDigest(String(fields.image_sha256))}` : ""}
        </span>
      ) : receipt.reason ? (
        <span className="text-slate-500">{receipt.reason}</span>
      ) : null}
      <span className="text-slate-400">{formatTime(receipt.collected_at)}</span>
    </div>
  );
}

export function HardwarePanel({ project }: { project: string }) {
  const client = useQueryClient();
  const projects = useProjects();
  const projectId = (projects.data ?? []).find((row) => row.name === project)?.id;
  const images = useHardwareImages(projectId);
  const receipts = useHardwareReceipts(projectId);
  const templates = useRunTemplates(projectId);
  const workspace = useWorkspace(projectId);
  const servers = useLiveServers();

  const physical = useMemo(() => physicalTemplates(templates.data), [templates.data]);
  const candidates = (workspace.data?.run_creation_options?.ssh_target_candidates ?? []) as { server_name: string; id?: string; ready?: boolean }[];
  const versions = workspace.data?.run_creation_options?.project_version_candidates ?? [];

  const [templateId, setTemplateId] = useState("");
  const [server, setServer] = useState("");
  const [deviceId, setDeviceId] = useState("");
  const [imageSha, setImageSha] = useState("");
  const [sequence, setSequence] = useState<"off_on" | "reset">("off_on");
  const [approval, setApproval] = useState<Approval | null>(null);

  const template = physical.find((item) => item.run_profile_id === templateId) ?? physical[0];
  const actionClass = template?.head_spec?.action_class ?? "";
  const effectiveServer = server || candidates[0]?.server_name || "";
  const devices = devicesOf(servers.data, effectiveServer);
  const effectiveDevice = deviceId || devices[0]?.id || "";
  const targetRevisionId = candidates.find((candidate) => candidate.server_name === effectiveServer)?.id ?? "";
  const versionId = versions[0]?.id ?? "";

  const body = () => ({
    project_version_id: versionId,
    template_selection: { kind: "run_profile_revision", run_profile_id: template?.run_profile_id ?? "" },
    parameter_overrides: {},
    dataset_selection: { kind: "none" },
    target_selection: { kind: "server_config_revision", server_config_revision_id: targetRevisionId },
    device_id: effectiveDevice,
    ...(actionClass === "program" ? { image_sha256: imageSha || null } : {}),
    ...(actionClass === "power" ? { power_sequence: sequence } : {}),
  });

  const preview = useMutation({
    mutationFn: () => api<{ plan_digest: string; hardware: Record<string, unknown> }>(`/api/v2/projects/${projectId}/hardware-action-previews`, { method: "POST", json: body() }),
  });
  const request = useMutation({
    mutationFn: async () => {
      const created = await api<{ approval_id: number }>(`/api/v2/projects/${projectId}/hardware-action-requests`, {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        json: { ...body(), expected_plan_digest: preview.data?.plan_digest },
      });
      return api<Approval>(`/api/v2/approvals/${created.approval_id}`);
    },
    onSuccess: (created) => {
      setApproval(created);
      void client.invalidateQueries({ queryKey: ["approvals"] });
    },
  });

  const missing: string[] = [];
  if (!template) missing.push("沒有燒錄／電源／實機測試模板（在執行設定新增 action_class 為 program／power／hil_test 的模板）");
  if (!versionId) missing.push("沒有已晉升的版本");
  if (!targetRevisionId) missing.push("目標機器沒有可用的設定版本");
  if (!effectiveDevice) missing.push("目標機器沒有宣告裝置（servers.yaml devices:）");
  if (actionClass === "program" && !imageSha) missing.push("燒錄需要選一個已登記的映像");
  const errorText = (error: unknown) => {
    if (error instanceof ApiError) {
      const reason = (error.details as { reason?: string } | undefined)?.reason;
      return `${error.message}${reason ? `（${reason}）` : ""}`;
    }
    return error ? (error as Error).message : null;
  };

  return (
    <div className="space-y-4 p-4">
      <Card className="space-y-2">
        <CardTitle>硬體實體動作</CardTitle>
        <p className="text-xs text-slate-500">燒錄／電源／實機測試永遠是一張獨立的核准卡，由另一個人核准；映像由平台推送到工作機、燒錄前再驗一次 digest。</p>
        {physical.length === 0 && !templates.isLoading ? <div className="text-sm text-slate-500">還沒有實體動作模板。</div> : null}
        <div className="grid gap-2 text-sm md:grid-cols-2">
          <label className="flex flex-col gap-1">
            <span className="text-xs text-slate-500">模板</span>
            <select className="rounded border border-slate-300 px-2 py-1" value={template?.run_profile_id ?? ""} onChange={(event) => { setTemplateId(event.target.value); setApproval(null); preview.reset(); }}>
              {physical.map((item) => (
                <option key={item.run_profile_id} value={item.run_profile_id}>
                  {item.name}（{ACTION_CLASS_LABELS[item.head_spec?.action_class ?? ""] ?? item.head_spec?.action_class}）
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs text-slate-500">執行機器</span>
            <select className="rounded border border-slate-300 px-2 py-1" value={effectiveServer} onChange={(event) => { setServer(event.target.value); setDeviceId(""); preview.reset(); }}>
              {candidates.map((candidate) => (
                <option key={candidate.server_name} value={candidate.server_name}>
                  {candidate.server_name}{candidate.ready === false ? "（未就緒）" : ""}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs text-slate-500">裝置</span>
            <select className="rounded border border-slate-300 px-2 py-1" value={effectiveDevice} onChange={(event) => { setDeviceId(event.target.value); preview.reset(); }}>
              {devices.map((device) => (
                <option key={device.id} value={device.id}>
                  {device.id}（{device.presence === "present" ? "在場" : device.presence === "absent" ? "不在場" : "未觀測"}）
                </option>
              ))}
            </select>
          </label>
          {actionClass === "program" ? (
            <label className="flex flex-col gap-1">
              <span className="text-xs text-slate-500">映像</span>
              <select className="rounded border border-slate-300 px-2 py-1" value={imageSha} onChange={(event) => { setImageSha(event.target.value); preview.reset(); }}>
                <option value="">選擇已登記的映像…</option>
                {(images.data ?? []).map((image) => (
                  <option key={image.id} value={image.sha256}>
                    {shortDigest(image.sha256)} · {image.kind} · {image.output_name}{image.known_good_marked_at ? " · known-good" : ""}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          {actionClass === "power" ? (
            <label className="flex flex-col gap-1">
              <span className="text-xs text-slate-500">電源序列</span>
              <select className="rounded border border-slate-300 px-2 py-1" value={sequence} onChange={(event) => { setSequence(event.target.value as "off_on" | "reset"); preview.reset(); }}>
                {POWER_SEQUENCES.map((item) => (
                  <option key={item.value} value={item.value}>{item.label}</option>
                ))}
              </select>
            </label>
          ) : null}
        </div>
        {devices.length > 0 ? (
          <div className="flex flex-wrap gap-1 text-xs">
            {devices.map((device) => (
              <Badge key={device.id} tone={presenceTone(device.presence)}>{device.id}：{device.presence}</Badge>
            ))}
          </div>
        ) : null}
        {missing.length > 0 ? <ul className="list-disc pl-5 text-xs text-amber-700">{missing.map((item) => <li key={item}>{item}</li>)}</ul> : null}
        <div className="flex flex-wrap items-center gap-2">
          <Button disabled={missing.length > 0 || preview.isPending} onClick={() => preview.mutate()}>
            預覽
          </Button>
          {preview.data ? (
            <span className="text-xs text-slate-600">
              {ACTION_CLASS_LABELS[String(preview.data.hardware?.action_class)] ?? ""} · {String(preview.data.hardware?.server_name)} · 裝置 {String(preview.data.hardware?.device_id)}
              {preview.data.hardware?.image_sha256 ? ` · 映像 ${shortDigest(String(preview.data.hardware.image_sha256))}` : ""}
            </span>
          ) : null}
          <Button variant="primary" disabled={!preview.data || request.isPending} onClick={() => request.mutate()}>
            送出核准卡
          </Button>
          {errorText(preview.error) ? <span className="text-xs text-rose-700">{errorText(preview.error)}</span> : null}
          {errorText(request.error) ? <span className="text-xs text-rose-700">{errorText(request.error)}</span> : null}
        </div>
        {approval ? <ApprovalCard approval={approval} onDecided={() => { setApproval(null); void receipts.refetch(); }} /> : null}
      </Card>

      <Card className="space-y-2">
        <CardTitle>映像登記表</CardTitle>
        {images.isLoading ? <div className="text-xs text-slate-400">載入中…</div> : null}
        {images.error ? <div className="text-xs text-rose-700">{(images.error as Error).message}</div> : null}
        {!images.isLoading && (images.data ?? []).length === 0 ? <div className="text-xs text-slate-500">還沒有登記的映像。跑一個 action_class 為 build、輸出宣告 artifact_class 的模板，結果回收後會自動登記。</div> : null}
        {(images.data ?? []).map((image) => (
          <ImageRow key={image.id} image={image} projectId={projectId ?? ""} onChanged={() => void images.refetch()} />
        ))}
      </Card>

      <Card className="space-y-2">
        <CardTitle>燒錄／測試收據</CardTitle>
        {receipts.isLoading ? <div className="text-xs text-slate-400">載入中…</div> : null}
        {receipts.error ? <div className="text-xs text-rose-700">{(receipts.error as Error).message}</div> : null}
        {!receipts.isLoading && (receipts.data ?? []).length === 0 ? <div className="text-xs text-slate-500">還沒有收據。實體動作的任務結束後，工作機寫的 hardware_receipt.json 會回收到這裡；沒有收據＝未知，不改任務結果。</div> : null}
        {(receipts.data ?? []).map((receipt) => (
          <ReceiptRow key={receipt.job_id} receipt={receipt} />
        ))}
      </Card>
    </div>
  );
}
