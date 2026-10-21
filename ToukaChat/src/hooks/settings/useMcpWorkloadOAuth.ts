"use client";

import { useCallback, useState } from "react";
import { fetchTouka, getActiveProxyUrl } from "@/lib/connections";
import { logSSOTError } from "@/lib/error_handler";
import {
  WorkloadCredentialStatusView,
  WorkloadDelegationInput,
  WorkloadDelegationView,
  WorkloadKeyRotationView,
  WorkloadMetadataView,
  WorkloadMutationView,
  WorkloadOAuthProfileView,
  WorkloadRecoveryView,
} from "@/lib/mcp_version";

export type McpWorkloadUiError = "request_failed";

export function useMcpWorkloadOAuth(agentId: string) {
  const proxyUrl = getActiveProxyUrl();
  const [profiles, setProfiles] = useState<WorkloadOAuthProfileView[]>([]);
  const [credentialStatuses, setCredentialStatuses] = useState<
    Record<string, WorkloadCredentialStatusView>
  >({});
  const [delegations, setDelegations] = useState<WorkloadDelegationView[]>([]);
  const [recovery, setRecovery] = useState<WorkloadRecoveryView[]>([]);
  const [rotation, setRotation] = useState<WorkloadKeyRotationView | null>(
    null,
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<McpWorkloadUiError | null>(null);

  const run = useCallback(
    async <T>(path: string, options?: RequestInit): Promise<T | null> => {
      setBusy(true);
      try {
        const response = await fetchTouka(proxyUrl, path, options);
        if (!response.ok) {
          setError("request_failed");
          return null;
        }
        const value = (await response.json()) as T;
        setError(null);
        return value;
      } catch (caught) {
        logSSOTError(caught, "useMcpWorkloadOAuth:request");
        setError("request_failed");
        return null;
      } finally {
        setBusy(false);
      }
    },
    [proxyUrl],
  );

  const refreshAdmin = useCallback(async (): Promise<boolean> => {
    const nextProfiles = await run<WorkloadOAuthProfileView[]>(
      "/v1/admin/mcp/oauth/workload/profiles",
    );
    if (nextProfiles === null) return false;
    setProfiles(nextProfiles);
    const statuses = await Promise.all(
      nextProfiles.map(async (profile) => {
        const status = await run<WorkloadCredentialStatusView>(
          `/v1/admin/mcp/oauth/workload/profiles/${encodeURIComponent(profile.profile_id)}/credential`,
        );
        return [profile.profile_id, status] as const;
      }),
    );
    setCredentialStatuses(
      Object.fromEntries(
        statuses.filter((entry) => entry[1] !== null),
      ) as Record<string, WorkloadCredentialStatusView>,
    );
    const [nextRecovery, nextRotation] = await Promise.all([
      run<WorkloadRecoveryView[]>("/v1/admin/mcp/oauth/workload/recovery"),
      run<WorkloadKeyRotationView>(
        "/v1/admin/mcp/oauth/workload/keys/rotation",
      ),
    ]);
    if (nextRecovery !== null) setRecovery(nextRecovery);
    if (nextRotation !== null) setRotation(nextRotation);
    return nextRecovery !== null && nextRotation !== null;
  }, [run]);

  const putProfile = useCallback(
    async (profile: WorkloadOAuthProfileView): Promise<boolean> => {
      const saved = await run<WorkloadOAuthProfileView>(
        `/v1/admin/mcp/oauth/workload/profiles/${encodeURIComponent(profile.profile_id)}`,
        { method: "PUT", body: JSON.stringify(profile) },
      );
      if (saved === null) return false;
      setProfiles((current) => [
        ...current.filter((item) => item.profile_id !== saved.profile_id),
        saved,
      ]);
      return true;
    },
    [run],
  );

  const deleteProfile = useCallback(
    async (profileId: string, generation: number): Promise<boolean> => {
      const result = await run<WorkloadMutationView>(
        `/v1/admin/mcp/oauth/workload/profiles/${encodeURIComponent(profileId)}?expected_generation=${generation}`,
        { method: "DELETE" },
      );
      if (result === null || result.status !== "completed") return false;
      setProfiles((current) =>
        current.filter((profile) => profile.profile_id !== profileId),
      );
      return true;
    },
    [run],
  );

  const setCredential = useCallback(
    async (
      profileId: string,
      clientId: string,
      clientSecret: string,
    ): Promise<boolean> => {
      const status = await run<WorkloadCredentialStatusView>(
        `/v1/admin/mcp/oauth/workload/profiles/${encodeURIComponent(profileId)}/credential`,
        {
          method: "PUT",
          body: JSON.stringify({
            client_id: clientId,
            client_secret: clientSecret,
          }),
        },
      );
      if (status === null) return false;
      setCredentialStatuses((current) => ({
        ...current,
        [profileId]: status,
      }));
      return true;
    },
    [run],
  );

  const revokeCredential = useCallback(
    async (profileId: string): Promise<boolean> => {
      const result = await run<WorkloadMutationView>(
        `/v1/admin/mcp/oauth/workload/profiles/${encodeURIComponent(profileId)}/credential/revoke`,
        { method: "POST" },
      );
      if (result === null) return false;
      setCredentialStatuses((current) => ({
        ...current,
        [profileId]: {
          profile_id: profileId,
          credential_id: current[profileId]?.credential_id ?? profileId,
          generation: result.generation,
          status: "tombstoned",
        },
      }));
      return result.status === "completed";
    },
    [run],
  );

  const refreshMetadata = useCallback(
    (profileId: string): Promise<WorkloadMetadataView | null> =>
      run<WorkloadMetadataView>(
        `/v1/admin/mcp/oauth/workload/profiles/${encodeURIComponent(profileId)}/metadata/refresh`,
        { method: "POST" },
      ),
    [run],
  );

  const startRotation = useCallback(async (): Promise<boolean> => {
    const next = await run<WorkloadKeyRotationView>(
      "/v1/admin/mcp/oauth/workload/keys/rotation",
      { method: "POST" },
    );
    if (next === null) return false;
    setRotation(next);
    return true;
  }, [run]);

  const retryRecovery = useCallback(async (): Promise<boolean> => {
    const next = await run<WorkloadRecoveryView[]>(
      "/v1/admin/mcp/oauth/workload/recovery/retry",
      { method: "POST" },
    );
    if (next === null) return false;
    setRecovery(next);
    return next.length === 0;
  }, [run]);

  const refreshDelegations = useCallback(async (): Promise<boolean> => {
    if (!agentId) {
      setDelegations([]);
      return false;
    }
    const next = await run<WorkloadDelegationView[]>(
      `/v1/mcp/agents/${encodeURIComponent(agentId)}/oauth/delegations`,
    );
    if (next === null) return false;
    setDelegations(next);
    return true;
  }, [agentId, run]);

  const putDelegation = useCallback(
    async (
      delegationId: string,
      payload: WorkloadDelegationInput,
    ): Promise<boolean> => {
      if (!agentId) return false;
      const saved = await run<WorkloadDelegationView>(
        `/v1/mcp/agents/${encodeURIComponent(agentId)}/oauth/delegations/${encodeURIComponent(delegationId)}`,
        { method: "PUT", body: JSON.stringify(payload) },
      );
      if (saved === null) return false;
      setDelegations((current) => [
        ...current.filter((item) => item.delegation_id !== saved.delegation_id),
        saved,
      ]);
      return true;
    },
    [agentId, run],
  );

  const deleteDelegation = useCallback(
    async (delegationId: string): Promise<boolean> => {
      if (!agentId) return false;
      const result = await run<WorkloadMutationView>(
        `/v1/mcp/agents/${encodeURIComponent(agentId)}/oauth/delegations/${encodeURIComponent(delegationId)}`,
        { method: "DELETE" },
      );
      if (result === null || result.status !== "completed") return false;
      setDelegations((current) =>
        current.filter((item) => item.delegation_id !== delegationId),
      );
      return true;
    },
    [agentId, run],
  );

  return {
    profiles,
    credentialStatuses,
    delegations,
    recovery,
    rotation,
    busy,
    error,
    refreshAdmin,
    putProfile,
    deleteProfile,
    setCredential,
    revokeCredential,
    refreshMetadata,
    retryRecovery,
    startRotation,
    refreshDelegations,
    putDelegation,
    deleteDelegation,
  };
}
