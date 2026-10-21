"use client";

import { useEffect, useState } from "react";
import { KeyRound, RefreshCw, ShieldAlert, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useConfirmToast } from "@/hooks/useConfirmToast";
import { useMcpWorkloadOAuth } from "@/hooks/settings/useMcpWorkloadOAuth";
import { WorkloadCredentialStatusView } from "@/lib/mcp_version";

interface CredentialFormProps {
  profileId: string;
  status?: WorkloadCredentialStatusView;
  busy: boolean;
  onSet: (
    profileId: string,
    clientId: string,
    clientSecret: string,
  ) => Promise<boolean>;
  onRevoke: (profileId: string) => Promise<boolean>;
  onRefreshMetadata: (profileId: string) => Promise<unknown>;
}

function CredentialForm({
  profileId,
  status,
  busy,
  onSet,
  onRevoke,
  onRefreshMetadata,
}: CredentialFormProps) {
  const { t } = useTranslation();
  const { confirmToast } = useConfirmToast();
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");

  const submit = async () => {
    if (!clientId || !clientSecret) return;
    try {
      await onSet(profileId, clientId, clientSecret);
    } finally {
      // 密鑰只活在本元件記憶體；成功與失敗都不保留、回填或持久化。
      setClientId("");
      setClientSecret("");
    }
  };

  return (
    <div className="border-border bg-muted/20 space-y-3 rounded-2xl border p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="text-foreground font-mono text-sm font-bold">
            {profileId}
          </p>
          <p className="text-muted-foreground text-xs">
            {t("settings.mcp_workload.credential_status", {
              status: status?.status ?? "missing",
              generation: status?.generation ?? "—",
            })}
          </p>
        </div>
        <div className="flex gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={busy}
            onClick={() => void onRefreshMetadata(profileId)}
          >
            <RefreshCw className="mr-1 h-3.5 w-3.5" />
            {t("settings.mcp_workload.refresh_metadata")}
          </Button>
          <Button
            type="button"
            variant="destructive"
            size="sm"
            disabled={busy || status?.status !== "active"}
            onClick={() =>
              confirmToast(t("settings.mcp_workload.confirm_revoke"), () =>
                onRevoke(profileId),
              )
            }
          >
            <Trash2 className="mr-1 h-3.5 w-3.5" />
            {t("settings.mcp_workload.revoke")}
          </Button>
        </div>
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        <div className="space-y-1.5">
          <Label htmlFor={`workload-client-id-${profileId}`}>Client ID</Label>
          <Input
            id={`workload-client-id-${profileId}`}
            type="password"
            autoComplete="off"
            value={clientId}
            onChange={(event) => setClientId(event.target.value)}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor={`workload-client-secret-${profileId}`}>
            Client Secret
          </Label>
          <Input
            id={`workload-client-secret-${profileId}`}
            type="password"
            autoComplete="new-password"
            value={clientSecret}
            onChange={(event) => setClientSecret(event.target.value)}
          />
        </div>
      </div>
      <Button
        type="button"
        disabled={busy || !clientId || !clientSecret}
        onClick={() => void submit()}
      >
        <KeyRound className="mr-2 h-4 w-4" />
        {t("settings.mcp_workload.save_credential")}
      </Button>
    </div>
  );
}

export function McpWorkloadAdminPanel() {
  const { t } = useTranslation();
  const { confirmToast } = useConfirmToast();
  const workload = useMcpWorkloadOAuth("");
  const { refreshAdmin } = workload;

  useEffect(() => {
    void refreshAdmin();
  }, [refreshAdmin]);

  return (
    <section className="space-y-5">
      <div>
        <h4 className="text-foreground text-sm font-black">
          {t("settings.mcp_workload.admin_title")}
        </h4>
        <p className="text-muted-foreground mt-1 text-xs">
          {t("settings.mcp_workload.admin_description")}
        </p>
      </div>

      {(workload.error || workload.recovery.length > 0) && (
        <div className="border-destructive/30 bg-destructive/10 text-destructive flex items-center gap-2 rounded-xl border p-3 text-xs">
          <ShieldAlert className="h-4 w-4 shrink-0" />
          <span className="flex-1">
            {workload.error
              ? t("settings.mcp_workload.request_failed")
              : t("settings.mcp_workload.recovery_required", {
                  count: workload.recovery.length,
                })}
          </span>
          {!workload.error && workload.recovery.length > 0 && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={workload.busy}
              onClick={() => void workload.retryRecovery()}
            >
              {t("settings.mcp_workload.retry_recovery")}
            </Button>
          )}
        </div>
      )}

      <div className="border-border flex flex-wrap items-center justify-between gap-3 rounded-2xl border p-4">
        <div>
          <p className="text-foreground text-sm font-bold">
            {t("settings.mcp_workload.key_rotation")}
          </p>
          <p className="text-muted-foreground text-xs">
            {workload.rotation?.phase ?? "—"}
          </p>
        </div>
        <Button
          type="button"
          variant="outline"
          disabled={workload.busy}
          onClick={() =>
            confirmToast(t("settings.mcp_workload.confirm_rotation"), () =>
              workload.startRotation(),
            )
          }
        >
          {t("settings.mcp_workload.start_rotation")}
        </Button>
      </div>

      <div className="space-y-3">
        {workload.profiles.length === 0 ? (
          <p className="text-muted-foreground rounded-2xl border p-5 text-sm">
            {t("settings.mcp_workload.no_profiles")}
          </p>
        ) : (
          workload.profiles.map((profile) => (
            <CredentialForm
              key={profile.profile_id}
              profileId={profile.profile_id}
              status={workload.credentialStatuses[profile.profile_id]}
              busy={workload.busy}
              onSet={workload.setCredential}
              onRevoke={workload.revokeCredential}
              onRefreshMetadata={workload.refreshMetadata}
            />
          ))
        )}
      </div>
    </section>
  );
}
