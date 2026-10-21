import { useState, useEffect } from "react";
import { useAuth } from "@/contexts/AuthContext";
import { getActiveProxyUrl, fetchTouka } from "@/lib/connections";
import { logSSOTError } from "@/lib/error_handler";

export interface SetupStatus {
  dispatch_token_is_default: boolean;
  auth_secret_persisted: boolean;
  api_keys_configured: string[];
  is_admin_temp_password: boolean;
  data_path_is_default: boolean;
  workspace_root_is_default: boolean;
  embedding_model: string;
}

export function useSetupStatus() {
  const { user } = useAuth();
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [fetchTrigger, setFetchTrigger] = useState(0);

  useEffect(() => {
    if (user?.role !== "admin") return;

    let cancelled = false;
    setIsLoading(true);

    (async () => {
      try {
        const res = await fetchTouka(getActiveProxyUrl(), "/v1/admin/setup/status");
        if (!cancelled && res.ok) {
          setStatus(await res.json());
        }
      } catch (e) {
        logSSOTError(e, "useSetupStatus");
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    })();

    return () => { cancelled = true; };
  }, [user?.id, user?.role, fetchTrigger]);

  return {
    status,
    isLoading,
    refetch: () => {
      setStatus(null);
      setFetchTrigger((t) => t + 1);
    },
  };
}
