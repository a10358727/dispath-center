import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { InstallPublicKeyDialog, looksLikePermissionDenied } from "./InstallPublicKeyDialog";

const response = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

afterEach(() => vi.unstubAllGlobals());

describe("InstallPublicKeyDialog", () => {
  it("only offers itself for key/permission failures", () => {
    expect(looksLikePermissionDenied(["SSH 到 5090 失敗: Permission denied for user user on host 1.2.3.4"])).toBe(true);
    expect(looksLikePermissionDenied(["SSH 到 5090 失敗: Permission denied (publickey)."])).toBe(true);
    expect(looksLikePermissionDenied(["connection timed out"])).toBe(false);
    expect(looksLikePermissionDenied(undefined)).toBe(false);
  });

  it("posts the password once, never shows it, and reports success", async () => {
    const bodies: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      bodies.push(String(init?.body));
      expect(String(input)).toBe("/api/v2/server-configs/5090/install-public-key");
      return response(200, { ok: true, outcome: "installed", detail: "x", public_key: "ssh-ed25519 AAAA dispatch" });
    }));
    const onInstalled = vi.fn();
    render(<InstallPublicKeyDialog name="5090" user="user" host="120.113.101.13" onInstalled={onInstalled} onClose={vi.fn()} />);

    expect(screen.getByRole("dialog", { name: "安裝公鑰" })).toHaveTextContent("user@120.113.101.13");
    const field = screen.getByLabelText("主機密碼") as HTMLInputElement;
    expect(field.type).toBe("password");
    expect(screen.getByRole("button", { name: "登入一次並安裝公鑰" })).toBeDisabled();
    fireEvent.change(field, { target: { value: "s3cret" } });
    fireEvent.click(screen.getByRole("button", { name: "登入一次並安裝公鑰" }));

    await waitFor(() => expect(onInstalled).toHaveBeenCalledTimes(1));
    expect(bodies).toEqual([JSON.stringify({ password: "s3cret" })]);
    expect(field.value).toBe("");
    expect(screen.getByRole("status")).toHaveTextContent("公鑰已安裝");
    expect(document.body.textContent).not.toContain("s3cret");
  });

  it("explains a rejected password and offers the manual key on unreachable hosts", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response(200, { ok: false, outcome: "auth_failed", detail: "password rejected" })));
    const onInstalled = vi.fn();
    render(<InstallPublicKeyDialog name="5090" user="user" host="h" onInstalled={onInstalled} onClose={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("主機密碼"), { target: { value: "wrong" } });
    fireEvent.click(screen.getByRole("button", { name: "登入一次並安裝公鑰" }));
    expect(await screen.findByRole("status")).toHaveTextContent("密碼被拒絕");
    expect(onInstalled).not.toHaveBeenCalled();

    vi.stubGlobal("fetch", vi.fn(async () => response(200, { ok: false, outcome: "unreachable", public_key: "ssh-ed25519 AAAA dispatch" })));
    fireEvent.change(screen.getByLabelText("主機密碼"), { target: { value: "again" } });
    fireEvent.click(screen.getByRole("button", { name: "登入一次並安裝公鑰" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("連不上主機"));
    expect(screen.getByText("ssh-ed25519 AAAA dispatch")).toBeInTheDocument();
  });

  it("surfaces a refused request (host identity not trusted) as an alert", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response(409, { error: { code: "ssh_host_identity_untrusted", message: "Trust the host identity before installing a public key" } })));
    render(<InstallPublicKeyDialog name="5090" user="user" host="h" onInstalled={vi.fn()} onClose={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("主機密碼"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "登入一次並安裝公鑰" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Trust the host identity");
  });
});
