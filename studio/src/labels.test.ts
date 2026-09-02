import { describe, expect, it } from "vitest";
import { actorLabel, describeAudit, promotionStateLabel, versionLabel } from "./labels";

describe("labels (整頓 U8)", () => {
  it("renders a version without exposing the full hash or raw state", () => {
    const label = versionLabel({ git_commit: "a".repeat(40), promotion_state: "promoted", created_at: "2026-09-02T00:00:00Z", git_ref: "main" }, 0);
    expect(label).toContain("#1");
    expect(label).toContain("main@");
    expect(label).toContain("已晉升");
    expect(label).not.toContain("a".repeat(40));
    expect(label).not.toContain("promoted");
    expect(promotionStateLabel(null)).toBe("觀察到");
  });

  it("describes an audit record in one Chinese line with paths redacted", () => {
    const line = describeAudit({ action: "enqueue", params: { project: "demo", job_id: 94, pin_server: "server-a", command: "python train.py --data /srv/data" } });
    expect(line).toBe("排入任務 · demo · 任務 #94 · server-a · python train.py --data …");
    expect(describeAudit({ action: "approval_decided", params: { kind: "engineering_task_promote", approval_id: 7 } })).toContain("卡 #7");
    expect(describeAudit({ action: "something_new" })).toBe("something_new");
    expect(actorLabel({ kind: "human", display_name: "Ada" })).toBe("Ada");
    expect(actorLabel(null)).toBe("系統");
  });
});
