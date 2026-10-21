import { useState, useEffect, useId } from "react";
import { useUserSettings } from "@/hooks/settings/useUserSettings";
import { useSecuritySettings } from "@/hooks/settings/useSecuritySettings";
import { User, UserCreate } from "@/lib/types";
import { Search, UserPlus, Edit, Trash2, Shield, User as UserIcon, Mail, Fingerprint, AtSign, Check, X, Key, Calendar, Clock } from "lucide-react";
import { cn } from "@/lib/utils";
import { avatarSrc } from "@/lib/connections";
import { formatToTaipeiDate } from "@/lib/time_utils";
import { useConfirmToast } from "@/hooks/useConfirmToast";
import { useTranslation } from "react-i18next";
import { useAuth } from "@/contexts/AuthContext";
import { useDirtyGuard } from "@/components/settings/SettingsDirtyContext";
import { AgentDataResetCard } from "@/components/settings/tabs/AgentDataResetCard";

export function UsersTab() {
    const { t } = useTranslation();
    const { users, isFetchingUsers, isSavingUser, fetchUsers, createUser, updateUser, deleteUser } = useUserSettings();
    const { allAgents, fetchSecurityData } = useSecuritySettings();
    const { confirmToast } = useConfirmToast();
    // P1 自我保護：取目前登入者，禁止對「自己」做角色降級/停權/刪除（後端為真守衛，前端防誤觸）。
    const { user: currentUser } = useAuth();
    const [search, setSearch] = useState("");
    const [editingUserId, setEditingUserId] = useState<string | null>(null);
    const [showCreateForm, setShowCreateForm] = useState(false);

    useEffect(() => {
        fetchUsers();
        fetchSecurityData();
    }, [fetchUsers, fetchSecurityData]);

    const handleUpdateUser = async (userId: string, data: Partial<User> & { password?: string }) => {
        const success = await updateUser(userId, data);
        if (success) setEditingUserId(null);
    };

    const handleCreateUser = async (data: UserCreate) => {
        const success = await createUser(data);
        if (success) setShowCreateForm(false);
    };

    const filteredUsers = users.filter(u => 
        u.email.toLowerCase().includes(search.toLowerCase()) || 
        (u.display_name || "").toLowerCase().includes(search.toLowerCase())
    );

    return (
        <div className="animate-in fade-in slide-in-from-bottom-2 space-y-6 duration-300">
            {/* Header Actions */}
            <div className="flex flex-col md:flex-row gap-4 items-center justify-between bg-muted/20 p-4 rounded-3xl border border-border">
                <div className="relative flex-1 w-full">
                    <Search className="text-muted-foreground absolute top-1/2 left-4 h-4 w-4 -translate-y-1/2" />
                    <input
                        type="text"
                        aria-label={t('settings.users_tab.search_placeholder')}
                        placeholder={t('settings.users_tab.search_placeholder')}
                        value={search}
                        onChange={e => setSearch(e.target.value)}
                        className="bg-background border-input focus:ring-primary/50 w-full rounded-2xl border py-3 pr-4 pl-12 text-sm transition-all outline-none focus:ring-2" 
                    />
                </div>
                <button 
                    onClick={() => setShowCreateForm(true)}
                    className="flex items-center gap-2 px-6 py-3 rounded-2xl bg-primary-button text-primary-foreground font-black text-sm hover:opacity-90 transition-all shadow-sm shrink-0 w-full md:w-auto justify-center"
                >
                    <UserPlus className="w-4 h-4" />
                    {t('settings.users_tab.add_user')}
                </button>
            </div>

            {/* Role legend：解釋人類使用者的兩種角色（admin / normal） */}
            <div className="flex flex-col sm:flex-row sm:flex-wrap gap-2 sm:gap-x-6 sm:gap-y-2 px-2 text-xs text-muted-foreground">
                <span className="font-black uppercase tracking-wider">{t('settings.users_tab.role_legend_title')}</span>
                <span className="flex items-center gap-1.5">
                    <span className="bg-primary/20 text-primary px-2 py-0.5 rounded-lg text-xs font-black uppercase tracking-wider flex items-center gap-1 shrink-0">
                        <Shield className="w-3 h-3" /> {t('settings.users_tab.role_badge_admin')}
                    </span>
                    {t('settings.users_tab.role_legend_admin')}
                </span>
                <span className="flex items-center gap-1.5">
                    <span className="bg-muted text-muted-foreground border border-border px-2 py-0.5 rounded-lg text-xs font-black uppercase tracking-wider shrink-0">
                        {t('settings.users_tab.form_role_option_normal')}
                    </span>
                    {t('settings.users_tab.role_legend_normal')}
                </span>
            </div>

            {/* Create Form */}
            {showCreateForm && (
                <UserForm 
                    onCancel={() => setShowCreateForm(false)} 
                    onSubmit={handleCreateUser}
                    isSaving={isSavingUser}
                    allAgents={allAgents.map(a => a.id)}
                />
            )}

            {/* User List */}
            <div className="grid gap-4">
                {isFetchingUsers ? (
                    <div className="flex justify-center p-12">
                        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary"></div>
                    </div>
                ) : filteredUsers.length === 0 ? (
                    <div className="text-center p-12 text-muted-foreground bg-muted/10 rounded-3xl border border-dashed border-border">
                        {t('settings.users_tab.no_users_found')}
                    </div>
                ) : (
                    filteredUsers.map(user => (
                        <div key={user.id}>
                            {editingUserId === user.id ? (
                                <UserForm
                                    user={user}
                                    onCancel={() => setEditingUserId(null)}
                                    onSubmit={(data) => handleUpdateUser(user.id, data)}
                                    isSaving={isSavingUser}
                                    allAgents={allAgents.map(a => a.id)}
                                    isSelf={user.id === currentUser?.id}
                                />
                            ) : (
                                <div className="group flex flex-col md:flex-row items-center gap-4 p-5 bg-muted/20 border border-border rounded-3xl hover:border-primary/40 transition-all">
                                    <div className="flex flex-1 items-center gap-4 w-full min-w-0">
                                        <div className="w-12 h-12 rounded-2xl bg-primary/10 flex items-center justify-center shrink-0 border border-primary/20 overflow-hidden">
                                            {user.avatar_url ? (
                                                <img src={avatarSrc(user.avatar_url)} alt={user.display_name || user.email} className="w-full h-full object-cover" />
                                            ) : (
                                                <UserIcon className="w-6 h-6 text-primary" />
                                            )}
                                        </div>
                                        <div className="flex flex-col min-w-0">
                                            <div className="flex items-center gap-2">
                                                <span className="font-black text-foreground text-base truncate">
                                                    {user.display_name || t('settings.users_tab.unnamed_user')}
                                                </span>
                                                {user.role === 'admin' && (
                                                    <span className="bg-primary/20 text-primary px-2 py-0.5 rounded-lg text-xs font-black uppercase tracking-wider flex items-center gap-1">
                                                        <Shield className="w-3 h-3" /> {t('settings.users_tab.role_badge_admin')}
                                                    </span>
                                                )}
                                                {user.status === 'suspended' && (
                                                    <span className="bg-destructive/10 text-destructive px-2 py-0.5 rounded-lg text-xs font-black uppercase tracking-wider">
                                                        {t('settings.users_tab.status_badge_suspended')}
                                                    </span>
                                                )}
                                            </div>
                                            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground font-medium mt-0.5">
                                                <span className="flex items-center gap-1"><Mail className="w-3 h-3" /> {user.email}</span>
                                                <span className="hidden md:flex items-center gap-1"><Fingerprint className="w-3 h-3" /> {user.id.slice(0, 8)}...</span>
                                                {user.created_at && (
                                                    <span className="hidden lg:flex items-center gap-1"><Calendar className="w-3 h-3" /> {t('settings.users_tab.created_at_label')} {formatToTaipeiDate(user.created_at * 1000)}</span>
                                                )}
                                                <span className="hidden lg:flex items-center gap-1"><Clock className="w-3 h-3" /> {t('settings.users_tab.last_active_label')} {user.last_active_at ? formatToTaipeiDate(user.last_active_at * 1000) : t('settings.users_tab.last_active_never')}</span>
                                            </div>
                                        </div>
                                    </div>

                                    {/* Assigned Agents */}
                                    <div className="flex flex-wrap gap-1 md:max-w-48 justify-start md:justify-end">
                                        {user.owned_agents?.map(agentId => (
                                            <span key={agentId} className="bg-muted px-2 py-0.5 rounded-md text-xs font-bold text-muted-foreground border border-border">
                                                {agentId}
                                            </span>
                                        ))}
                                        {(!user.owned_agents || user.owned_agents.length === 0) && (
                                            <span className="text-xs text-muted-foreground font-bold uppercase italic">{t('settings.users_tab.empty_state_no_agents')}</span>
                                        )}
                                    </div>

                                    <div className="flex items-center gap-2 shrink-0 w-full md:w-auto justify-end">
                                        <button 
                                            onClick={() => setEditingUserId(user.id)}
                                            className="p-2.5 rounded-xl border border-border bg-background text-muted-foreground hover:border-primary hover:text-primary transition-all"
                                            title={t('settings.users_tab.edit_user_tooltip')}
                                        >
                                            <Edit className="w-4 h-4" />
                                        </button>
                                        {user.id !== currentUser?.id && (
                                        <button
                                            onClick={() => {
                                                confirmToast(
                                                    t('settings.users_tab.delete_confirm_message'),
                                                    () => deleteUser(user.id),
                                                    t('settings.users_tab.delete_confirm_title'),
                                                );
                                            }}
                                            className="p-2.5 rounded-xl border border-border bg-background text-muted-foreground hover:bg-destructive hover:text-destructive-foreground hover:border-destructive transition-all"
                                            title={t('settings.users_tab.delete_user_tooltip')}
                                        >
                                            <Trash2 className="w-4 h-4" />
                                        </button>
                                        )}
                                    </div>
                                </div>
                            )}
                        </div>
                    ))
                )}
            </div>

            {/* ADR-0113：Per-Agent 資料重置（清執行期資料、保留身分；admin only）*/}
            <AgentDataResetCard />
        </div>
    );
}

interface UserFormData {
    email: string;
    password: string;
    display_name: string;
    avatar_url: string;
    bio: string;
    role: string;
    status: string;
    owned_agents: string[];
}

interface UserFormProps {
    user?: User;
    onSubmit: (data: UserFormData) => void;
    onCancel: () => void;
    isSaving: boolean;
    allAgents: string[];
    /** P1：編輯的是自己 → 鎖住角色/狀態，避免自我降級/停權（與後端末位-admin 守衛同向）。 */
    isSelf?: boolean;
}

function UserForm({ user, onSubmit, onCancel, isSaving, allAgents, isSelf }: UserFormProps) {
    const { t } = useTranslation();
    // P3 a11y：每個 UserForm 實例唯一 id 前綴，供 label htmlFor/input id 關聯（建立/各列編輯表單各自唯一）。
    const formId = useId();
    const [formData, setFormData] = useState<UserFormData>({
        email: user?.email || "",
        password: "",
        display_name: user?.display_name || "",
        avatar_url: user?.avatar_url || "",
        bio: user?.bio || "",
        role: user?.role || "normal",
        status: user?.status || "active",
        owned_agents: user?.owned_agents || []
    });
    // A5 表單驗證：email 必填+格式；密碼建立時必填且 ≥8、編輯時留空=不變（有填則 ≥8）。
    const [errors, setErrors] = useState<{ email?: string; password?: string }>({});

    // C5：未存草稿守衛——欄位與原值（編輯）或空白（建立）不同即 dirty。
    const _orig = {
        email: user?.email || "", display_name: user?.display_name || "",
        avatar_url: user?.avatar_url || "", bio: user?.bio || "",
        role: user?.role || "normal", status: user?.status || "active",
        owned_agents: user?.owned_agents || [],
    };
    const isDirty =
        formData.email !== _orig.email || formData.password !== "" ||
        formData.display_name !== _orig.display_name || formData.avatar_url !== _orig.avatar_url ||
        formData.bio !== _orig.bio || formData.role !== _orig.role || formData.status !== _orig.status ||
        formData.owned_agents.length !== _orig.owned_agents.length ||
        formData.owned_agents.some((a: string) => !_orig.owned_agents.includes(a));
    useDirtyGuard(`user-form-${formId}`, isDirty);

    const validate = (): boolean => {
        const next: { email?: string; password?: string } = {};
        const email = formData.email.trim();
        if (!email) next.email = t('settings.users_tab.form_error_email_required');
        else if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) next.email = t('settings.users_tab.form_error_email_invalid');
        if (!user) {
            if (!formData.password) next.password = t('settings.users_tab.form_error_password_required');
            else if (formData.password.length < 8) next.password = t('settings.users_tab.form_error_password_too_short');
        } else if (formData.password && formData.password.length < 8) {
            next.password = t('settings.users_tab.form_error_password_too_short');
        }
        setErrors(next);
        return Object.keys(next).length === 0;
    };

    const handleSubmit = () => {
        if (validate()) onSubmit(formData);
    };

    const toggleAgent = (agentId: string) => {
        setFormData((prev: UserFormData) => ({
            ...prev,
            owned_agents: prev.owned_agents.includes(agentId)
                ? prev.owned_agents.filter((id: string) => id !== agentId)
                : [...prev.owned_agents, agentId]
        }));
    };

    return (
        <div className="bg-muted/40 p-6 rounded-3xl border border-primary/30 space-y-6 shadow-xl animate-in zoom-in-95 duration-200">
            <div className="flex items-center justify-between border-b border-border pb-4">
                <h4 className="text-base font-black flex items-center gap-2">
                    {user ? <Edit className="w-4 h-4" /> : <UserPlus className="w-4 h-4" />}
                    {user ? t('settings.users_tab.form_edit_title') : t('settings.users_tab.form_create_title')}
                </h4>
                <div className="flex gap-2">
                    <button onClick={onCancel} className="p-2 hover:bg-muted rounded-xl transition-all">
                        <X className="w-5 h-5" />
                    </button>
                </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                <div className="space-y-4">
                    <div className="space-y-1.5">
                        <label htmlFor={`${formId}-email`} className="text-xs font-black uppercase text-muted-foreground px-1 tracking-wider">{t('settings.users_tab.form_email_label')}</label>
                        <div className="relative">
                            <Mail className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                            <input
                                id={`${formId}-email`}
                                type="email"
                                value={formData.email}
                                onChange={e => setFormData({...formData, email: e.target.value})}
                                placeholder="example@touka.io"
                                aria-invalid={!!errors.email}
                                className="w-full bg-background border-input rounded-xl border py-2.5 pl-10 pr-4 text-sm font-bold focus:ring-2 focus:ring-primary/50 outline-none transition-all"
                            />
                        </div>
                        {errors.email && <p className="text-xs font-bold text-destructive px-1">{errors.email}</p>}
                    </div>

                    <div className="space-y-1.5">
                        <label htmlFor={`${formId}-password`} className="text-xs font-black uppercase text-muted-foreground px-1 tracking-wider">{t('settings.users_tab.form_password_label')} {user && t('settings.users_tab.password_keep_hint')}</label>
                        <div className="relative">
                            <Key className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                            <input
                                id={`${formId}-password`}
                                type="password"
                                value={formData.password}
                                onChange={e => setFormData({...formData, password: e.target.value})}
                                placeholder={user ? "********" : t('settings.users_tab.password_placeholder')}
                                aria-invalid={!!errors.password}
                                className="w-full bg-background border-input rounded-xl border py-2.5 pl-10 pr-4 text-sm font-bold focus:ring-2 focus:ring-primary/50 outline-none transition-all"
                            />
                        </div>
                        {errors.password && <p className="text-xs font-bold text-destructive px-1">{errors.password}</p>}
                    </div>

                    <div className="space-y-1.5">
                        <label htmlFor={`${formId}-display`} className="text-xs font-black uppercase text-muted-foreground px-1 tracking-wider">{t('settings.users_tab.form_display_name_label')}</label>
                        <div className="relative">
                            <AtSign className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                            <input
                                id={`${formId}-display`}
                                type="text"
                                value={formData.display_name}
                                onChange={e => setFormData({...formData, display_name: e.target.value})}
                                placeholder={t('settings.users_tab.display_name_placeholder')}
                                className="w-full bg-background border-input rounded-xl border py-2.5 pl-10 pr-4 text-sm font-bold focus:ring-2 focus:ring-primary/50 outline-none transition-all"
                            />
                        </div>
                    </div>

                    <div className="flex gap-4">
                        <div className="flex-1 space-y-1.5">
                            <label htmlFor={`${formId}-role`} className="text-xs font-black uppercase text-muted-foreground px-1 tracking-wider">{t('settings.users_tab.form_role_label')}</label>
                            <select
                                id={`${formId}-role`}
                                value={formData.role}
                                onChange={e => setFormData({...formData, role: e.target.value})}
                                disabled={isSelf}
                                className="w-full bg-background border-input rounded-xl border py-2.5 px-4 text-sm font-bold outline-none focus:ring-2 focus:ring-primary/50 transition-all appearance-none disabled:opacity-50 disabled:cursor-not-allowed"
                            >
                                <option value="normal">{t('settings.users_tab.form_role_option_normal')}</option>
                                <option value="admin">{t('settings.users_tab.form_role_option_admin')}</option>
                            </select>
                        </div>
                        <div className="flex-1 space-y-1.5">
                            <label htmlFor={`${formId}-status`} className="text-xs font-black uppercase text-muted-foreground px-1 tracking-wider">{t('settings.users_tab.form_status_label')}</label>
                            <select
                                id={`${formId}-status`}
                                value={formData.status}
                                onChange={e => setFormData({...formData, status: e.target.value})}
                                disabled={isSelf}
                                className="w-full bg-background border-input rounded-xl border py-2.5 px-4 text-sm font-bold outline-none focus:ring-2 focus:ring-primary/50 transition-all appearance-none disabled:opacity-50 disabled:cursor-not-allowed"
                            >
                                <option value="active">{t('settings.users_tab.form_status_option_active')}</option>
                                <option value="suspended">{t('settings.users_tab.form_status_option_suspended')}</option>
                            </select>
                        </div>
                    </div>
                </div>

                <div className="space-y-4">
                    <div className="space-y-1.5">
                        <label className="text-xs font-black uppercase text-muted-foreground px-1 tracking-wider flex items-center justify-between">
                            {t('settings.users_tab.form_assigned_agents_label')}
                            <span className="text-xs font-bold text-primary">{t('settings.users_tab.agents_selected_count', { count: formData.owned_agents.length })}</span>
                        </label>
                        <div className="bg-background border border-border rounded-2xl p-3 h-48 overflow-y-auto custom-scrollbar space-y-1">
                            {allAgents.length === 0 ? (
                                <div className="text-xs text-muted-foreground italic p-2">{t('settings.users_tab.no_agents_available')}</div>
                            ) : allAgents.map(agentId => (
                                <button
                                    key={agentId}
                                    onClick={() => toggleAgent(agentId)}
                                    aria-pressed={formData.owned_agents.includes(agentId)}
                                    className={cn(
                                        "w-full text-left px-3 py-2 rounded-xl text-xs font-bold transition-all flex items-center justify-between group",
                                        formData.owned_agents.includes(agentId) 
                                            ? "bg-primary/10 text-primary border border-primary/20 shadow-sm" 
                                            : "text-muted-foreground hover:bg-muted/50 border border-transparent"
                                    )}
                                >
                                    {agentId}
                                    {formData.owned_agents.includes(agentId) && <Check className="w-3 h-3" />}
                                </button>
                            ))}
                        </div>
                    </div>

                    <div className="space-y-1.5">
                        <label htmlFor={`${formId}-bio`} className="text-xs font-black uppercase text-muted-foreground px-1 tracking-wider">{t('settings.users_tab.form_bio_label')}</label>
                        <textarea
                            id={`${formId}-bio`}
                            value={formData.bio}
                            onChange={e => setFormData({...formData, bio: e.target.value})}
                            placeholder={t('settings.users_tab.bio_placeholder')}
                            className="w-full bg-background border-input rounded-xl border py-2.5 px-4 text-sm font-medium focus:ring-2 focus:ring-primary/50 outline-none transition-all h-20 resize-none"
                        />
                    </div>
                </div>
            </div>

            <div className="flex justify-end gap-3 pt-4 border-t border-border">
                <button 
                    onClick={onCancel}
                    className="px-6 py-2.5 rounded-xl border border-border font-black text-sm text-muted-foreground hover:bg-muted transition-all"
                >
                    {t('settings.users_tab.cancel')}
                </button>
                <button
                    onClick={handleSubmit}
                    disabled={isSaving}
                    className="flex items-center gap-2 px-8 py-2.5 rounded-xl bg-primary-button text-primary-foreground font-black text-sm shadow-md hover:opacity-90 disabled:opacity-50 transition-all"
                >
                    {isSaving && <RefreshCw className="w-4 h-4 animate-spin" />}
                    {t('settings.users_tab.save_changes')}
                </button>
            </div>
        </div>
    );
}

function RefreshCw({ className }: { className?: string }) {
    return <RefreshCwIcon className={className} />;
}
import { RefreshCw as RefreshCwIcon } from "lucide-react";
