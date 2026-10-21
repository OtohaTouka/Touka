"use client";

import { useEffect, useState } from "react";
import { Link2, ShieldAlert, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useMcpWorkloadOAuth } from "@/hooks/settings/useMcpWorkloadOAuth";
import { useConfirmToast } from "@/hooks/useConfirmToast";
import { getSystemNow } from "@/lib/time_utils";

interface Props {
  agentId: string;
}

export function McpOwnerDelegationPanel({ agentId }: Props) {
  const { t } = useTranslation();
  const { confirmToast } = useConfirmToast();
  const workload = useMcpWorkloadOAuth(agentId);
  const { refreshDelegations } = workload;
  const [delegationId, setDelegationId] = useState("");
  const [installationId, setInstallationId] = useState("");
  const [installationGeneration, setInstallationGeneration] = useState("1");
  const [toolName, setToolName] = useState("");
  const [scopes, setScopes] = useState("");
  const [expiresHours, setExpiresHours] = useState("24");

  useEffect(() => {
    void refreshDelegations();
  }, [refreshDelegations]);

  const submit = async () => {
    const generation = Number(installationGeneration);
    const hours = Number(expiresHours);
    const requiredScopes = scopes.split(/\s+/).filter(Boolean);
    if (
      !delegationId ||
      !installationId ||
      !toolName ||
      !Number.isInteger(generation) ||
      generation < 1 ||
      !Number.isFinite(hours) ||
      hours <= 0 ||
      hours > 720 ||
      requiredScopes.length === 0
    ) {
      return;
    }
    const saved = await workload.putDelegation(delegationId, {
      installation_id: installationId,
      installation_generation: generation,
      raw_tool_name: toolName,
      required_scopes: requiredScopes,
      expires_at: getSystemNow().getTime() / 1000 + hours * 3600,
    });
    if (saved) {
      setDelegationId("");
      setInstallationId("");
      setInstallationGeneration("1");
      setToolName("");
      setScopes("");
      setExpiresHours("24");
    }
  };

  if (!agentId) return null;

  return (
    <section className="space-y-4">
      <div>
        <h4 className="text-foreground flex items-center gap-2 text-sm font-bold">
          <Link2 className="text-primary h-4 w-4" />
          {t("settings.general_tab.mcp_delegation_title")}
        </h4>
        <p className="text-muted-foreground mt-1 text-xs">
          {t("settings.general_tab.mcp_delegation_description", { agentId })}
        </p>
      </div>

      {workload.error && (
        <div className="border-destructive/30 bg-destructive/10 text-destructive flex gap-2 rounded-xl border p-3 text-xs">
          <ShieldAlert className="h-4 w-4 shrink-0" />
          {t("settings.general_tab.mcp_delegation_denied")}
        </div>
      )}

      <div className="border-border bg-muted/20 grid gap-3 rounded-2xl border p-4 md:grid-cols-2">
        <div className="space-y-1.5">
          <Label htmlFor="mcp-delegation-id">Delegation ID</Label>
          <Input
            id="mcp-delegation-id"
            value={delegationId}
            onChange={(event) => setDelegationId(event.target.value)}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="mcp-installation-id">Installation ID</Label>
          <Input
            id="mcp-installation-id"
            value={installationId}
            onChange={(event) => setInstallationId(event.target.value)}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="mcp-installation-generation">
            Installation generation
          </Label>
          <Input
            id="mcp-installation-generation"
            type="number"
            min={1}
            value={installationGeneration}
            onChange={(event) => setInstallationGeneration(event.target.value)}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="mcp-tool-name">Tool name</Label>
          <Input
            id="mcp-tool-name"
            value={toolName}
            onChange={(event) => setToolName(event.target.value)}
          />
        </div>
        <div className="space-y-1.5 md:col-span-2">
          <Label htmlFor="mcp-delegation-scopes">Scopes</Label>
          <Input
            id="mcp-delegation-scopes"
            value={scopes}
            onChange={(event) => setScopes(event.target.value)}
            placeholder={t("settings.general_tab.mcp_delegation_scopes_hint")}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="mcp-delegation-hours">
            {t("settings.general_tab.mcp_delegation_hours")}
          </Label>
          <Input
            id="mcp-delegation-hours"
            type="number"
            min={1}
            max={720}
            value={expiresHours}
            onChange={(event) => setExpiresHours(event.target.value)}
          />
        </div>
        <div className="flex items-end">
          <Button
            type="button"
            disabled={workload.busy}
            onClick={() => void submit()}
          >
            {t("settings.general_tab.mcp_delegation_create")}
          </Button>
        </div>
      </div>

      <div className="space-y-2">
        {workload.delegations.map((delegation) => (
          <div
            key={delegation.delegation_id}
            className="border-border flex flex-wrap items-center justify-between gap-3 rounded-xl border p-3"
          >
            <div>
              <p className="text-foreground font-mono text-sm font-bold">
                {delegation.delegation_id}
              </p>
              <p className="text-muted-foreground text-xs">
                {delegation.installation_id} / {delegation.raw_tool_name} · {delegation.status}
              </p>
            </div>
            <Button
              type="button"
              variant="destructive"
              size="sm"
              disabled={workload.busy}
              onClick={() =>
                confirmToast(
                  t("settings.general_tab.mcp_delegation_confirm_delete"),
                  () => workload.deleteDelegation(delegation.delegation_id),
                )
              }
            >
              <Trash2 className="mr-1 h-3.5 w-3.5" />
              {t("settings.general_tab.mcp_delegation_delete")}
            </Button>
          </div>
        ))}
      </div>
    </section>
  );
}
