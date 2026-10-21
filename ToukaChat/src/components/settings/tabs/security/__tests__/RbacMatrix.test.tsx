import { useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type {
  McpServerStatus,
  PermissionConfig,
  Skill,
  Tool,
} from "@/lib/types";
import { RbacMatrix } from "../RbacMatrix";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

const SYSTEM_TOOLS: Tool[] = [
  { name: "read_file", description: "Read a file", category: "Builtin" },
];

const MCP_SERVERS: McpServerStatus[] = [
  {
    name: "stripe",
    status: "connected",
    tools_count: 2,
    url: "https://mcp.stripe.com",
  },
];

const SKILLS: Skill[] = [
  {
    id: "python_expert",
    name: "Python Expert",
    description: "",
    system_prompt: "",
    required_tools: [],
    mcp_servers: [],
  },
];

function createPermissions(): PermissionConfig {
  return {
    roles: {
      Owner: {
        desc: "Owner role",
        allowed_tools: ["*"],
        denied_tools: [],
        allowed_mcp_servers: ["*"],
        allowed_skills: ["*"],
      },
    },
    agent_mappings: {},
    system_config: {
      default_role: "Base",
      allow_unregistered_agents: false,
    },
  };
}

function renderMatrix(
  initialPermissions = createPermissions(),
  onChange = vi.fn<(permissions: PermissionConfig) => void>(),
) {
  const onSave = vi
    .fn<(permissions: PermissionConfig) => Promise<boolean>>()
    .mockResolvedValue(true);

  function Harness() {
    const [permissions, setPermissions] = useState(initialPermissions);

    return (
      <RbacMatrix
        permissions={permissions}
        setPermissions={(nextPermissions) => {
          if (!nextPermissions) return;
          onChange(nextPermissions);
          setPermissions(nextPermissions);
        }}
        isSavingPermissions={false}
        savePermissions={onSave}
        systemTools={SYSTEM_TOOLS}
        mcpServers={MCP_SERVERS}
        skills={SKILLS}
      />
    );
  }

  render(<Harness />);
  return { onChange, onSave };
}

describe("RbacMatrix — P3 開放式授權", () => {
  it("三個授權軸都有可操作的開放式 toggle", () => {
    renderMatrix();

    const switches = screen.getAllByRole("switch");
    expect(switches).toHaveLength(3);
    switches.forEach((toggle) => {
      expect(toggle.getAttribute("data-state")).toBe("checked");
    });

    const explicitChoices = screen.getAllByRole("checkbox");
    expect(explicitChoices).toHaveLength(3);
    explicitChoices.forEach((choice) => {
      expect(choice.hasAttribute("disabled")).toBe(true);
      expect(choice.getAttribute("data-state")).toBe("checked");
    });
  });

  it("Owner 可由 * 切成明確工具清單，再切回 *，且不改動傳入物件", () => {
    const initialPermissions = createPermissions();
    const originalSnapshot = structuredClone(initialPermissions);
    const { onChange, onSave } = renderMatrix(initialPermissions);

    const builtinToggle = screen.getAllByRole("switch")[0];
    fireEvent.click(builtinToggle);

    expect(onChange.mock.calls.at(-1)?.[0].roles.Owner.allowed_tools).toEqual(
      [],
    );
    expect(initialPermissions).toEqual(originalSnapshot);

    const builtinChoice = screen.getAllByRole("checkbox")[0];
    expect(builtinChoice.hasAttribute("disabled")).toBe(false);
    fireEvent.click(builtinChoice);
    expect(onChange.mock.calls.at(-1)?.[0].roles.Owner.allowed_tools).toEqual([
      "read_file",
    ]);
    fireEvent.click(
      screen.getByRole("button", { name: "settings.rbac_matrix.save_button" }),
    );
    expect(onSave.mock.calls.at(-1)?.[0].roles.Owner.allowed_tools).toEqual([
      "read_file",
    ]);

    fireEvent.click(screen.getAllByRole("switch")[0]);
    expect(onChange.mock.calls.at(-1)?.[0].roles.Owner.allowed_tools).toEqual([
      "*",
    ]);
    fireEvent.click(
      screen.getByRole("button", { name: "settings.rbac_matrix.save_button" }),
    );
    expect(onSave.mock.calls.at(-1)?.[0].roles.Owner.allowed_tools).toEqual([
      "*",
    ]);
  });

  it("MCP 與技能 toggle 只更新自己的 wire 欄位", () => {
    const { onChange } = renderMatrix();

    fireEvent.click(screen.getAllByRole("switch")[1]);
    const afterMcp = onChange.mock.calls.at(-1)?.[0].roles.Owner;
    if (!afterMcp) throw new Error('Expected Owner permissions after the MCP toggle');
    expect(afterMcp.allowed_mcp_servers).toEqual([]);
    expect(afterMcp.allowed_tools).toEqual(["*"]);
    expect(afterMcp.allowed_skills).toEqual(["*"]);

    fireEvent.click(screen.getAllByRole("switch")[2]);
    const afterSkills = onChange.mock.calls.at(-1)?.[0].roles.Owner;
    if (!afterSkills) throw new Error('Expected Owner permissions after the skills toggle');
    expect(afterSkills.allowed_mcp_servers).toEqual([]);
    expect(afterSkills.allowed_tools).toEqual(["*"]);
    expect(afterSkills.allowed_skills).toEqual([]);
  });
});
