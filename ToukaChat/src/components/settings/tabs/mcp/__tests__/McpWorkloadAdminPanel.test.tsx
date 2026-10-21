import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const setCredential = vi.fn();
const refreshAdmin = vi.fn();

vi.mock("@/hooks/settings/useMcpWorkloadOAuth", () => ({
  useMcpWorkloadOAuth: () => ({
    profiles: [
      {
        profile_id: "calendar",
        enabled: true,
        expected_issuer: "https://issuer.example",
        expected_resource: "https://mcp.example",
        token_endpoint_auth_method: "client_secret_basic",
        workload_principal_id: "heartbeat:Alice",
        owner_user_id: "owner-1",
        execution_agent_id: "Alice",
        generation: 1,
        credential_id: "calendar-secret",
        allow_private_network: false,
        private_network_cidrs: [],
      },
    ],
    credentialStatuses: {},
    recovery: [],
    rotation: null,
    busy: false,
    error: null,
    refreshAdmin,
    setCredential,
    revokeCredential: vi.fn(),
    refreshMetadata: vi.fn(),
    startRotation: vi.fn(),
  }),
}));

import { McpWorkloadAdminPanel } from "../McpWorkloadAdminPanel";

describe("McpWorkloadAdminPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setCredential.mockResolvedValue(true);
    refreshAdmin.mockResolvedValue(true);
  });

  it("送出後立刻清除兩個 secret input，不回填 masked 或明文", async () => {
    render(<McpWorkloadAdminPanel />);
    const clientId = screen.getByLabelText("Client ID");
    const clientSecret = screen.getByLabelText("Client Secret");
    fireEvent.change(clientId, { target: { value: "client-canary" } });
    fireEvent.change(clientSecret, { target: { value: "secret-canary" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存憑證" }));

    await waitFor(() =>
      expect(setCredential).toHaveBeenCalledWith(
        "calendar",
        "client-canary",
        "secret-canary",
      ),
    );
    expect(clientId).toHaveValue("");
    expect(clientSecret).toHaveValue("");
    expect(screen.queryByDisplayValue(/canary/)).not.toBeInTheDocument();
  });
});
