import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { HostIdentityPanel } from "./HostIdentityPanel";

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

afterEach(() => vi.unstubAllGlobals());

describe("HostIdentityPanel", () => {
  it("requires an explicit OOB fingerprint or explicit TOFU acknowledgement", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => response(
      String(input).endsWith("/observe")
        ? { algorithm: "ssh-ed25519", fingerprint_sha256: "SHA256:observed" }
        : { state: "trusted" },
    ));
    vi.stubGlobal("fetch", fetchMock);
    render(<HostIdentityPanel name="rental-a" identity={null} onChanged={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "Observe current host key" }));
    expect(await screen.findByText(/SHA256:observed/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Verify provider fingerprint/ })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Provider SHA256 fingerprint"), { target: { value: "SHA256:observed" } });
    fireEvent.click(screen.getByRole("button", { name: /Verify provider fingerprint/ }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/v2/server-configs/rental-a/host-identity/actions/trust",
      expect.objectContaining({ body: JSON.stringify({ verification_method: "oob", expected_fingerprint_sha256: "SHA256:observed" }) }),
    );
  });

  it("labels mismatch as blocked and offers Replace Identity", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response({ algorithm: "ssh-ed25519", fingerprint_sha256: "SHA256:new" })));
    render(<HostIdentityPanel name="rental-a" identity={{ state: "mismatch", algorithm: "ssh-ed25519", fingerprint_sha256: "SHA256:old", mismatch_fingerprint_sha256: "SHA256:new" }} onChanged={vi.fn()} />);
    expect(screen.getByText("Blocked — host identity changed")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Observe current host key" }));
    expect(await screen.findByText(/SHA256:new/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /& replace/i })).toBeDisabled();
  });
});
