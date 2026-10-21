import { useState, useCallback } from 'react';
import { User, UserCreate } from '@/lib/types';
import { getActiveProxyUrl, fetchTouka } from '@/lib/connections';
import { logSSOTError, getErrorMessage, errorMsg } from '@/lib/error_handler';
import { getSystemNow } from '@/lib/time_utils';
import { toast } from 'sonner';
import { useTranslation } from "react-i18next";

export function useUserSettings() {
    const { t } = useTranslation();
    const [users, setUsers] = useState<User[]>([]);
    const [isFetchingUsers, setIsFetchingUsers] = useState(false);
    const [isSavingUser, setIsSavingUser] = useState(false);

    const fetchUsers = useCallback(async () => {
        setIsFetchingUsers(true);
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, "/v1/admin/users");
            if (res.ok) {
                setUsers(await res.json());
            } else if (res.status === 403) {
                // Not an admin, ignore silently or handle
            } else {
                toast.error(errorMsg("USER_LIST_FETCH_FAILED"));
            }
        } catch (e) {
            logSSOTError(e, "useUserSettings:fetchUsers");
        } finally {
            setIsFetchingUsers(false);
        }
    }, []);

    const createUser = async (data: UserCreate) => {
        setIsSavingUser(true);
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, "/v1/admin/users", {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data)
            });
            if (res.ok) {
                toast.success(t('hooks.use_user_settings.user_created_success'));
                await fetchUsers();
                return true;
            } else {
                const errData = await res.json().catch(() => ({}));
                const errMsg = errData.detail?.message || await getErrorMessage(errData, "SYSTEM_ERROR");
                toast.error(errMsg);
                return false;
            }
        } catch (err) {
            logSSOTError(err, "useUserSettings:createUser");
            toast.error(errorMsg("SYSTEM_ERROR"));
            return false;
        } finally {
            setIsSavingUser(false);
        }
    };

    const updateUser = async (userId: string, data: Partial<User> & { password?: string }) => {
        setIsSavingUser(true);
        const proxyUrl = getActiveProxyUrl();
        try {
            // 修復 0（2026-07-11 空密碼事故）：空密碼欄＝「保持不變」，送出前剝除——
            // 編輯表單的 formData 恆帶 password:""，過去原樣進 PATCH body，後端看鍵
            // 不看值即把密碼覆蓋成 bcrypt("")。後端已同步改為空值忽略（雙層防禦）。
            const { password, ...rest } = data;
            const payload = password ? { ...rest, password } : rest;
            const res = await fetchTouka(proxyUrl, `/v1/admin/users/${userId}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            if (res.ok) {
                toast.success(t('hooks.use_user_settings.user_updated_success'));
                await fetchUsers();
                return true;
            } else {
                const errData = await res.json().catch(() => ({}));
                const errMsg = errData.detail?.message || await getErrorMessage(errData, "SYSTEM_ERROR");
                toast.error(errMsg);
                return false;
            }
        } catch (err) {
            logSSOTError(err, "useUserSettings:updateUser");
            toast.error(errorMsg("SYSTEM_ERROR"));
            return false;
        } finally {
            setIsSavingUser(false);
        }
    };

    // 自助更新「自己的」個人檔案（PATCH /v1/auth/me）——任何登入者皆可,身分取自 JWT。
    // 與 admin updateUser 分離(SRP):不帶 userId、成功後不呼叫 admin-only 的 fetchUsers()。
    const updateOwnProfile = async (data: Partial<Pick<User, 'display_name' | 'avatar_url' | 'bio'>>) => {
        setIsSavingUser(true);
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, "/v1/auth/me", {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data)
            });
            if (res.ok) {
                toast.success(t('hooks.use_user_settings.user_updated_success'));
                return true;
            } else {
                const errData = await res.json().catch(() => ({}));
                const errMsg = errData.detail?.message || await getErrorMessage(errData, "SYSTEM_ERROR");
                toast.error(errMsg);
                return false;
            }
        } catch (err) {
            logSSOTError(err, "useUserSettings:updateOwnProfile");
            toast.error(errorMsg("SYSTEM_ERROR"));
            return false;
        } finally {
            setIsSavingUser(false);
        }
    };

    // ADR-0106：上傳自己的頭像圖片（multipart POST /v1/auth/avatar）。身分取自 JWT，無 IDOR。
    const uploadOwnAvatar = async (file: File) => {
        setIsSavingUser(true);
        const proxyUrl = getActiveProxyUrl();
        try {
            const form = new FormData();
            form.append("file", file);
            const res = await fetchTouka(proxyUrl, "/v1/auth/avatar", { method: 'POST', body: form });
            if (res.ok) {
                toast.success(t('hooks.use_user_settings.user_updated_success'));
                return true;
            }
            const errData = await res.json().catch(() => ({}));
            toast.error(errData.detail?.message || await getErrorMessage(errData, "SYSTEM_ERROR"));
            return false;
        } catch (err) {
            logSSOTError(err, "useUserSettings:uploadOwnAvatar");
            toast.error(errorMsg("SYSTEM_ERROR"));
            return false;
        } finally {
            setIsSavingUser(false);
        }
    };

    // ADR-0106：刪除自己的頭像檔並清空 avatar_url（DELETE /v1/auth/avatar）。
    const deleteOwnAvatar = async () => {
        setIsSavingUser(true);
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, "/v1/auth/avatar", { method: 'DELETE' });
            if (res.ok) {
                toast.success(t('hooks.use_user_settings.user_updated_success'));
                return true;
            }
            toast.error(errorMsg("SYSTEM_ERROR"));
            return false;
        } catch (err) {
            logSSOTError(err, "useUserSettings:deleteOwnAvatar");
            toast.error(errorMsg("SYSTEM_ERROR"));
            return false;
        } finally {
            setIsSavingUser(false);
        }
    };

    // ADR-0107：自助清空個人資料（profile + 頭像 + 我擁有專案的設定列;保留實體/agent/帳號）。
    // 需密碼重輸（後端閘）。成功回 true 並提示清除的專案設定數。
    const clearMyData = async (currentPassword: string) => {
        setIsSavingUser(true);
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, "/v1/auth/clear-my-data", {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ current_password: currentPassword }),
            });
            if (res.ok) {
                const report = await res.json().catch(() => ({}));
                toast.success(
                    t('hooks.use_user_settings.clear_data_success', {
                        count: report?.cleared?.project_settings ?? 0,
                    }),
                );
                return true;
            }
            const errData = await res.json().catch(() => ({}));
            toast.error(errData.detail?.message || await getErrorMessage(errData, "SYSTEM_ERROR"));
            return false;
        } catch (err) {
            logSSOTError(err, "useUserSettings:clearMyData");
            toast.error(errorMsg("SYSTEM_ERROR"));
            return false;
        } finally {
            setIsSavingUser(false);
        }
    };

    // 軌2 ADR-0110：自助匯出本人完整資料為加密 ZIP（POST /v1/auth/export-my-data）。
    // 需密碼重輸 + 加密密語（≥12，後端閘）。成功則觸發瀏覽器下載並回 true。
    const exportMyData = async (currentPassword: string, passphrase: string) => {
        setIsSavingUser(true);
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, "/v1/auth/export-my-data", {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ current_password: currentPassword, passphrase }),
            });
            if (res.ok) {
                const blob = await res.blob();
                const url = URL.createObjectURL(blob);
                const a = document.createElement("a");
                a.href = url;
                a.download = `touka-data-export-${getSystemNow().toISOString().slice(0, 10)}.zip`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
                toast.success(t('hooks.use_user_settings.export_data_success'));
                return true;
            }
            const errData = await res.json().catch(() => ({}));
            toast.error(errData.detail?.message || await getErrorMessage(errData, "SYSTEM_ERROR"));
            return false;
        } catch (err) {
            logSSOTError(err, "useUserSettings:exportMyData");
            toast.error(errorMsg("SYSTEM_ERROR"));
            return false;
        } finally {
            setIsSavingUser(false);
        }
    };

    const deleteUser = async (userId: string) => {
        setIsSavingUser(true);
        const proxyUrl = getActiveProxyUrl();
        try {
            const res = await fetchTouka(proxyUrl, `/v1/admin/users/${userId}`, {
                method: 'DELETE'
            });
            if (res.ok) {
                toast.success(t('hooks.use_user_settings.user_deleted_success'));
                await fetchUsers();
                return true;
            } else {
                const errData = await res.json().catch(() => ({}));
                const errMsg = errData.detail?.message || await getErrorMessage(errData, "SYSTEM_ERROR");
                toast.error(errMsg);
                return false;
            }
        } catch (err) {
            logSSOTError(err, "useUserSettings:deleteUser");
            toast.error(errorMsg("SYSTEM_ERROR"));
            return false;
        } finally {
            setIsSavingUser(false);
        }
    };

    return {
        users,
        isFetchingUsers,
        isSavingUser,
        fetchUsers,
        createUser,
        updateUser,
        updateOwnProfile,
        uploadOwnAvatar,
        deleteOwnAvatar,
        clearMyData,
        exportMyData,
        deleteUser
    };
}
