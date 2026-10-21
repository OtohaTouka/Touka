import { useTranslation } from "react-i18next";
import {
  ShieldCheck,
  ShieldOff,
  Save,
  RefreshCw,
  Wrench,
  Box,
  Zap,
  Shield,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type {
  PermissionConfig,
  Tool,
  McpServerStatus,
  Skill,
} from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Card } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { TagInput } from "@/components/ui/tag-input";

type RoleListField =
  | "allowed_tools"
  | "allowed_mcp_servers"
  | "allowed_skills"
  | "denied_mcp_tools";

interface GrantItem {
  id: string;
  label: string;
}

interface OpenEndedGrantListProps {
  axisLabel: string;
  icon: LucideIcon;
  items: GrantItem[];
  values: string[];
  onChange: (values: string[]) => void;
}

interface Props {
  permissions: PermissionConfig | null;
  setPermissions: (p: PermissionConfig | null) => void;
  isSavingPermissions: boolean;
  savePermissions: (p: PermissionConfig) => Promise<boolean>;
  systemTools: Tool[];
  mcpServers: McpServerStatus[];
  skills: Skill[];
}

function withRoleList(
  permissions: PermissionConfig,
  roleName: string,
  field: RoleListField,
  values: string[],
): PermissionConfig {
  return {
    ...permissions,
    roles: {
      ...permissions.roles,
      [roleName]: {
        ...permissions.roles[roleName],
        [field]: values,
      },
    },
  };
}

function updateExplicitGrant(
  values: string[],
  itemId: string,
  checked: boolean,
): string[] {
  const remaining = values.filter((value) => value !== "*" && value !== itemId);
  return checked ? [...remaining, itemId] : remaining;
}

function OpenEndedGrantList({
  axisLabel,
  icon: Icon,
  items,
  values,
  onChange,
}: OpenEndedGrantListProps) {
  const { t } = useTranslation();
  const isOpenEnded = values.includes("*");
  const openEndedLabel = t("settings.rbac_matrix.open_ended_label");

  return (
    <div className="space-y-3">
      <h5 className="text-muted-foreground flex items-center gap-2 pl-1 text-xs font-black tracking-widest uppercase">
        <Icon size={12} /> {axisLabel}
      </h5>
      <div className="border-border bg-background/50 flex items-start justify-between gap-3 rounded-2xl border p-3">
        <div className="space-y-1">
          <p className="text-foreground text-xs font-bold">{openEndedLabel}</p>
          <p className="text-muted-foreground text-xs leading-relaxed">
            {t("settings.rbac_matrix.open_ended_hint")}
          </p>
        </div>
        <Switch
          aria-label={`${axisLabel}: ${openEndedLabel}`}
          checked={isOpenEnded}
          onCheckedChange={(checked) => onChange(checked ? ["*"] : [])}
        />
      </div>
      <div className="custom-scrollbar border-border bg-background/50 max-h-40 space-y-1 overflow-y-auto rounded-2xl border p-3">
        {items.map((item) => (
          <label
            key={item.id}
            className="group/tool hover:bg-accent/50 flex cursor-pointer items-center gap-2 rounded-lg p-2 transition-colors"
          >
            <Checkbox
              checked={isOpenEnded || values.includes(item.id)}
              disabled={isOpenEnded}
              onCheckedChange={(checked) =>
                onChange(updateExplicitGrant(values, item.id, checked === true))
              }
            />
            <span className="text-foreground group-hover/tool:text-primary text-xs font-medium transition-colors">
              {item.label}
            </span>
          </label>
        ))}
      </div>
    </div>
  );
}

export function RbacMatrix({
  permissions,
  setPermissions,
  isSavingPermissions,
  savePermissions,
  systemTools,
  mcpServers,
  skills,
}: Props) {
  const { t } = useTranslation();
  if (!permissions) return null;

  return (
    <section className="space-y-6">
      <div className="flex items-center justify-between">
        <div className="space-y-1">
          <h4 className="text-foreground flex items-center gap-2 text-sm font-bold">
            <ShieldCheck className="text-primary h-5 w-5" />{" "}
            {t("settings.rbac_matrix.title")}
          </h4>
          <p className="text-muted-foreground text-xs">
            {t("settings.rbac_matrix.description")}
          </p>
        </div>
        <Button
          onClick={() => permissions && savePermissions(permissions)}
          disabled={isSavingPermissions || !permissions}
          className="bg-primary-button text-primary-foreground shadow-primary/20 rounded-xl px-6 py-2 text-xs font-bold shadow-lg transition-all hover:opacity-90"
        >
          {isSavingPermissions ? (
            <RefreshCw className="mr-2 h-3 w-3 animate-spin" />
          ) : (
            <Save className="mr-2 h-4 w-4" />
          )}
          {t("settings.rbac_matrix.save_button")}
        </Button>
      </div>

      <div className="grid grid-cols-1 gap-6">
        {Object.entries(permissions.roles).map(([roleName, roleCfg]) => (
          <Card
            key={roleName}
            className="group border-border bg-muted/20 relative space-y-6 overflow-hidden rounded-3xl border p-6"
          >
            <div className="relative z-10 flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div className="bg-primary/10 text-primary flex h-10 w-10 items-center justify-center rounded-2xl text-lg font-black uppercase">
                  {roleName[0]}
                </div>
                <div>
                  <div className="text-foreground text-base font-black tracking-tight uppercase">
                    {roleName}
                  </div>
                  <div className="text-muted-foreground font-mono text-xs">
                    {roleCfg.desc || t("settings.rbac_matrix.custom_role_desc")}
                  </div>
                </div>
              </div>
              {/* default_auth_level 旋鈕已拆除（hitl-unification H1）：後端從未消費該
                  欄位＝無作用旋鈕；HITL 等級改由聊天列 AuthSwitcher 逐 session 設定。 */}
            </div>

            <div className="relative z-10 grid grid-cols-1 gap-6 md:grid-cols-3">
              {/* Builtin Tools */}
              <OpenEndedGrantList
                axisLabel={t("settings.rbac_matrix.section.builtin_tools")}
                icon={Wrench}
                items={systemTools
                  .filter((tool) => !tool.category?.startsWith("MCP"))
                  .map((tool) => ({
                    id: tool.name,
                    label: tool.name.includes("__")
                      ? tool.name.split("__")[1]
                      : tool.name,
                  }))}
                values={roleCfg.allowed_tools ?? []}
                onChange={(values) =>
                  setPermissions(
                    withRoleList(
                      permissions,
                      roleName,
                      "allowed_tools",
                      values,
                    ),
                  )
                }
              />

              {/* MCP Servers */}
              <div className="space-y-3">
                <OpenEndedGrantList
                  axisLabel={t("settings.rbac_matrix.section.mcp_servers")}
                  icon={Box}
                  items={mcpServers.map((server) => ({
                    id: server.name,
                    label: server.name,
                  }))}
                  values={roleCfg.allowed_mcp_servers ?? []}
                  onChange={(values) =>
                    setPermissions(
                      withRoleList(
                        permissions,
                        roleName,
                        "allowed_mcp_servers",
                        values,
                      ),
                    )
                  }
                />

                {/* ADR-0195 軸 B：工具粒度的拒絕清單。上方勾選是「掛不掛這台」，這裡才是
                    「這台的哪個工具不給」。拒絕優先，且對 Owner 同樣生效。 */}
                <div className="space-y-2 pt-1">
                  <h5 className="text-muted-foreground flex items-center gap-2 pl-1 text-xs font-black tracking-widest uppercase">
                    <ShieldOff size={12} />{" "}
                    {t("settings.rbac_matrix.section.denied_mcp_tools")}
                  </h5>
                  <TagInput
                    tags={roleCfg.denied_mcp_tools ?? []}
                    placeholder={t(
                      "settings.rbac_matrix.denied_mcp_tools_placeholder",
                    )}
                    onChange={(tags) =>
                      setPermissions(
                        withRoleList(
                          permissions,
                          roleName,
                          "denied_mcp_tools",
                          tags,
                        ),
                      )
                    }
                  />
                  <p className="text-muted-foreground pl-1 text-xs leading-relaxed opacity-70">
                    {t("settings.rbac_matrix.denied_mcp_tools_hint")}
                  </p>
                </div>
              </div>

              {/* Skills */}
              <OpenEndedGrantList
                axisLabel={t("settings.rbac_matrix.section.skills")}
                icon={Zap}
                items={skills.map((skill) => ({
                  id: skill.id,
                  label: skill.name,
                }))}
                values={roleCfg.allowed_skills ?? []}
                onChange={(values) =>
                  setPermissions(
                    withRoleList(
                      permissions,
                      roleName,
                      "allowed_skills",
                      values,
                    ),
                  )
                }
              />
            </div>
            <div className="text-primary/5 absolute top-0 right-0 -mt-4 -mr-4 p-8 transition-transform duration-700 group-hover:scale-110">
              <Shield size={120} fill="currentColor" />
            </div>
          </Card>
        ))}
      </div>
    </section>
  );
}
