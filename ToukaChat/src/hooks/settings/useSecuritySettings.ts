import { useState, useCallback } from 'react';
import { PermissionConfig, AutonomyConfig, IdentityMap, AgentIdentity } from '@/lib/types';
import { getActiveProxyUrl, fetchTouka } from '@/lib/connections';
import { logSSOTError, errorMsg } from '@/lib/error_handler';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';

export function useSecuritySettings() {
    const { t } = useTranslation();
    const [permissions, setPermissions] = useState<PermissionConfig | null>(null);
    const [identityMap, setIdentityMap] = useState<IdentityMap | null>(null);
    const [allAgents, setAllAgents] = useState<AgentIdentity[]>([]);
    const [autonomyConfig, setAutonomyConfig] = useState<AutonomyConfig | null>(null);

    const [isSavingPermissions, setIsSavingPermissions] = useState(false);
    const [isSavingIdentity, setIsSavingIdentity] = useState(false);
    const [isSavingAutonomy, setIsSavingAutonomy] = useState(false);

    const fetchSecurityData = useCallback(async () => {
        const proxyUrl = getActiveProxyUrl();
        try {
            const [permsRes, identRes, agentsRes, autoRes] = await Promise.allSettled([
                fetchTouka(proxyUrl, "/v1/permissions"),
                fetchTouka(proxyUrl, "/v1/identity"),
                fetchTouka(proxyUrl, "/v1/identity/agents"),
                fetchTouka(proxyUrl, "/v1/autonomy")
            ]);

            if (permsRes.status === 'fulfilled' && permsRes.value.ok) setPermissions(await permsRes.value.json());
            if (identRes.status === 'fulfilled' && identRes.value.ok) setIdentityMap(await identRes.value.json());
            if (agentsRes.status === 'fulfilled' && agentsRes.value.ok) setAllAgents(await agentsRes.value.json());
            if (autoRes.status === 'fulfilled' && autoRes.value.ok) setAutonomyConfig(await autoRes.value.json());
        } catch (e) {
            logSSOTError(e, "useSecuritySettings:fetchSecurityData");
        }
    }, []);

    const savePermissions = async (newPerms: PermissionConfig) => {
        setIsSavingPermissions(true);
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, "/v1/permissions", {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(newPerms)
            });
            if (res.ok) {
                setPermissions(newPerms);
                toast.success(t('hooks.use_security_settings.permissions_saved_success'));
                return true;
            } else {
                toast.error(errorMsg("CONFIG_SAVE_FAILED"));
                return false;
            }
        } catch (err) {
            logSSOTError(err, "useSecuritySettings:savePermissions");
            toast.error(errorMsg("SYSTEM_ERROR"));
            return false;
        } finally {
            setIsSavingPermissions(false);
        }
    };

    const saveIdentity = async (newMap: IdentityMap) => {
        setIsSavingIdentity(true);
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, "/v1/identity", {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(newMap)
            });
            if (res.ok) {
                setIdentityMap(newMap);
                toast.success(t('hooks.use_security_settings.identity_saved_success'));
                return true;
            } else {
                toast.error(errorMsg("CONFIG_SAVE_FAILED"));
                return false;
            }
        } catch (err) {
            logSSOTError(err, "useSecuritySettings:saveIdentity");
            toast.error(errorMsg("SYSTEM_ERROR"));
            return false;
        } finally {
            setIsSavingIdentity(false);
        }
    };

    const saveAutonomy = async (newAuto: AutonomyConfig) => {
        setIsSavingAutonomy(true);
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, "/v1/autonomy", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(newAuto)
            });
            if (res.ok) {
                setAutonomyConfig(newAuto);
                toast.success(t('hooks.use_security_settings.autonomy_updated_success'));
                return true;
            } else {
                toast.error(errorMsg("CONFIG_SAVE_FAILED"));
                return false;
            }
        } catch (err) {
            logSSOTError(err, "useSecuritySettings:saveAutonomy");
            toast.error(errorMsg("SYSTEM_ERROR"));
            return false;
        } finally {
            setIsSavingAutonomy(false);
        }
    };

    return {
        permissions, setPermissions,
        identityMap, setIdentityMap,
        allAgents, setAllAgents,
        autonomyConfig, setAutonomyConfig,
        isSavingPermissions, isSavingIdentity, isSavingAutonomy,
        fetchSecurityData, savePermissions, saveIdentity, saveAutonomy
    };
}
