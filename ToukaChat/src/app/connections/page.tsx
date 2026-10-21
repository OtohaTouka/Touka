// src/app/connections/page.tsx
"use client";

import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useLocalStorage } from "@/hooks/useLocalStorage";
import LeftSidebar from "@/components/LeftSidebar";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Checkbox } from "@/components/ui/checkbox";
import { PlusCircle, Pencil, Trash2, Shield, Radio, Globe, Zap, Cpu } from "lucide-react";
import { getSystemEpoch } from "@/lib/time_utils";
import { PROVIDER_CATALOG, PROVIDER_IDS, resolvePrefillUrl, type ProviderKey } from "@/lib/providerCatalog";
import { applyConnectionEdit } from "@/lib/connectionEdit";
import { Connection } from "@/lib/types";
import { DirectConnectionNotice } from "@/components/settings/tabs/connections/DirectConnectionNotice";

export default function ConnectionsPage() {
    const { t } = useTranslation();
    // useLocalStorage 統一管理 JSON 序列化與持久化（LOCALSTORAGE_SSOT）
    const [connections, setConnections] = useLocalStorage<Connection[]>("touka_connections", []);
    const [newConnName, setNewConnName] = useState("");
    const [newConnUrl, setNewConnUrl] = useState("");
    const [newConnApiKey, setNewConnApiKey] = useState("");
    const [newConnProvider, setNewConnProvider] = useState<string>("proxy");
    const [editingId, setEditingId] = useState<string | null>(null);
    const [changeKey, setChangeKey] = useState(false);

    const handleProviderChange = (next: string) => {
        setNewConnProvider(next);
        setNewConnUrl(prev => resolvePrefillUrl(next, prev));
    };

    const saveConnections = (conns: Connection[]) => {
        setConnections(conns); // useLocalStorage 自動持久化
        // Broadcast update for other components
        window.dispatchEvent(new CustomEvent("touka_connections_updated", { detail: conns }));
    };

    const addConnection = () => {
        if (!newConnUrl) return;
        const newConns = [...connections, {
            id: "conn-" + getSystemEpoch(),
            name: newConnName || (newConnProvider === 'proxy' ? "Touka Proxy" : "New Connection"),
            url: newConnUrl,
            apiKey: newConnApiKey,
            provider: newConnProvider,
            protocol: "openai",
            enabled: true
        }];
        saveConnections(newConns);
        setNewConnName("");
        setNewConnUrl("");
        setNewConnApiKey("");
    };

    const startEdit = (conn: Connection) => {
        setEditingId(conn.id);
        setNewConnName(conn.name);
        setNewConnUrl(conn.url);
        setNewConnProvider(conn.provider);
        setNewConnApiKey("");   // 安全：永不回填既有金鑰
        setChangeKey(false);
    };

    const cancelEdit = () => {
        setEditingId(null);
        setNewConnName("");
        setNewConnUrl("");
        setNewConnApiKey("");
        setNewConnProvider("proxy");
        setChangeKey(false);
    };

    const submitConnection = () => {
        if (!newConnUrl) return;
        if (editingId) {
            const existing = connections.find(c => c.id === editingId);
            if (existing) {
                // 金鑰保留邏輯交給共用純函式（changeKey=false 時保留既有金鑰）。
                const updated = applyConnectionEdit(existing, {
                    name: newConnName,
                    url: newConnUrl,
                    provider: newConnProvider,
                    protocol: existing.protocol || "openai",
                    apiKey: newConnApiKey,
                }, changeKey);
                saveConnections(connections.map(c => c.id === editingId ? updated : c));
            }
            cancelEdit();
            return;
        }
        addConnection();
    };

    const toggleConnection = (id: string) => {
        saveConnections(connections.map(c => c.id === id ? { ...c, enabled: !c.enabled } : c));
    };

    const removeConnection = (id: string) => {
        if (editingId === id) cancelEdit();
        saveConnections(connections.filter(c => c.id !== id));
    };

    const getIcon = (provider: string) => {
        switch (provider) {
            case 'proxy': return <Shield className="w-5 h-5 text-primary" />;
            case 'openai': return <Globe className="w-5 h-5 text-status-success" />;
            case 'ollama': return <Cpu className="w-5 h-5 text-muted-foreground" />;
            case 'openrouter': return <Zap className="w-5 h-5 text-status-warning" />;
            default: return <Radio className="w-5 h-5 text-primary" />;
        }
    };

    return (
        <>
            <LeftSidebar />
            <main id="main-content" className="flex-1 flex min-w-0 bg-white dark:bg-card relative overflow-hidden">
                <div className="flex-1 overflow-y-auto custom-scrollbar">
                    <div className="max-w-4xl mx-auto py-12 px-8">

                        <header className="mb-12">
                            <div className="flex items-center gap-4 mb-4">
                                <div className="p-3 bg-primary/10 rounded-2xl">
                                    <Radio className="w-8 h-8 text-primary animate-pulse" />
                                </div>
                                <div>
                                    <h1 className="text-4xl font-black text-foreground tracking-tight">{t('app.connections_page.title')}</h1>
                                    <p className="text-muted-foreground font-medium">Configure and bridge Touka with external intelligence models.</p>
                                </div>
                            </div>
                        </header>

                        <div className="space-y-8">
                            {/* New Connection Section */}
                            <section className="bg-background border border-border rounded-3xl p-8 shadow-sm">
                                <h3 className="text-lg font-bold mb-6 flex items-center gap-2">
                                    {editingId ? <Pencil className="w-5 h-5 text-primary" /> : <PlusCircle className="w-5 h-5 text-primary" />}
                                    {editingId ? t('connections.edit.editing_badge', { name: newConnName }) : t('app.connections_page.add_ai_node')}
                                </h3>
                                <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mb-6">
                                    <div className="space-y-2">
                                        <label className="text-xs font-black uppercase text-muted-foreground tracking-widest pl-1">{t('app.connections_page.node_name_label')}</label>
                                        <Input
                                            placeholder={t('app.connections_page.node_name_placeholder')}
                                            value={newConnName}
                                            onChange={e => setNewConnName(e.target.value)}
                                            className="h-12 bg-muted/20 border-none rounded-xl"
                                        />
                                    </div>
                                    <div className="space-y-2">
                                        <label className="text-xs font-black uppercase text-muted-foreground tracking-widest pl-1">{t('app.connections_page.provider_label')}</label>
                                        <select
                                            value={newConnProvider}
                                            onChange={e => handleProviderChange(e.target.value)}
                                            className="w-full h-12 bg-muted/20 border-none rounded-xl px-4 text-sm focus:ring-2 focus:ring-primary outline-none"
                                        >
                                            {PROVIDER_IDS.map((id) => (
                                                <option key={id} value={id}>
                                                    {PROVIDER_CATALOG[id].icon} {t(`${PROVIDER_CATALOG[id].i18nBase}.label`)}
                                                </option>
                                            ))}
                                        </select>
                                    </div>
                                    <div className="space-y-2 md:col-span-2">
                                        <label className="text-xs font-black uppercase text-muted-foreground tracking-widest pl-1">{t('app.connections_page.api_endpoint_url_label')}</label>
                                        <Input
                                            placeholder={newConnProvider === 'proxy' ? t('app.connections_page.api_url_placeholder_proxy') : t('app.connections_page.api_url_placeholder_openai')}
                                            value={newConnUrl}
                                            onChange={e => setNewConnUrl(e.target.value)}
                                            className="h-12 bg-muted/20 border-none rounded-xl"
                                        />
                                        {PROVIDER_CATALOG[newConnProvider as ProviderKey] && (
                                            <p className="text-xs text-muted-foreground italic pl-1 mt-1">
                                                {t(`${PROVIDER_CATALOG[newConnProvider as ProviderKey].i18nBase}.hint`)}{' '}
                                                <a
                                                    href={PROVIDER_CATALOG[newConnProvider as ProviderKey].docUrl}
                                                    target="_blank"
                                                    rel="noopener noreferrer"
                                                    className="text-primary not-italic font-medium hover:underline"
                                                >
                                                    {t('connections.providers.doc_link_label')}
                                                </a>
                                            </p>
                                        )}
                                        <DirectConnectionNotice provider={newConnProvider} />
                                    </div>
                                    <div className="space-y-2 md:col-span-2">
                                        <label className="text-xs font-black uppercase text-muted-foreground tracking-widest pl-1">{t('app.connections_page.api_key_label')}</label>
                                        {editingId && (
                                            <label className="flex items-center gap-2 pl-1 text-xs font-medium text-muted-foreground cursor-pointer">
                                                <Checkbox checked={changeKey} onCheckedChange={(c) => setChangeKey(!!c)} />
                                                {t('connections.edit.change_key_label')}
                                            </label>
                                        )}
                                        {(!editingId || changeKey) ? (
                                            <Input
                                                type="password"
                                                placeholder="sk-..."
                                                value={newConnApiKey}
                                                onChange={e => setNewConnApiKey(e.target.value)}
                                                className="h-12 bg-muted/20 border-none rounded-xl"
                                            />
                                        ) : (
                                            <p className="text-xs text-muted-foreground italic pl-1">{t('connections.edit.key_kept_hint')}</p>
                                        )}
                                    </div>
                                </div>
                                <div className="flex gap-3">
                                    <Button onClick={submitConnection} className="flex-1 py-6 bg-primary-button text-white font-black rounded-2xl shadow-xl shadow-primary/20 hover:scale-100 transition-transform">
                                        {editingId ? t('connections.edit.save') : t('app.connections_page.deploy_connection_button')}
                                    </Button>
                                    {editingId && (
                                        <Button onClick={cancelEdit} variant="outline" className="py-6 px-8 font-black rounded-2xl">
                                            {t('connections.edit.cancel')}
                                        </Button>
                                    )}
                                </div>
                            </section>

                            {/* Connection List */}
                            <div className="space-y-4">
                                <h3 className="text-sm font-black uppercase text-muted-foreground tracking-widest pl-2">{t('app.connections_page.existing_nodes_heading', { count: connections.length })}</h3>
                                {connections.length === 0 ? (
                                    <div className="p-12 border-2 border-dashed border-border rounded-3xl flex flex-col items-center justify-center text-muted-foreground">
                                        <Radio className="w-12 h-12 mb-4 opacity-20" />
                                        <p className="font-bold">{t('app.connections_page.empty_state_title')}</p>
                                        <p className="text-xs">{t('app.connections_page.empty_state_hint')}</p>
                                    </div>
                                ) : (
                                    <div className="grid grid-cols-1 gap-4">
                                        {connections.map(conn => (
                                            <div key={conn.id} className={`group bg-card border ${conn.enabled ? 'border-primary/30 shadow-lg shadow-primary/5' : 'border-border'} rounded-2xl p-6 transition-all`}>
                                                <div className="flex items-center justify-between">
                                                    <div className="flex items-center gap-4">
                                                        <div className={`p-3 rounded-xl ${conn.enabled ? 'bg-primary/10' : 'bg-muted/50'}`}>
                                                            {getIcon(conn.provider)}
                                                        </div>
                                                        <div>
                                                            <h4 className="font-bold text-foreground leading-tight">{conn.name}</h4>
                                                            <p className="text-xs text-muted-foreground font-mono truncate w-48 md:max-w-md mt-1">{conn.url}</p>
                                                        </div>
                                                    </div>
                                                    <div className="flex items-center gap-3">
                                                        <div className="flex items-center gap-2 mr-4">
                                                            <span className={`text-xs font-black uppercase tracking-widest ${conn.enabled ? 'text-primary' : 'text-muted-foreground'}`}>
                                                                {conn.enabled ? 'Online' : 'Offline'}
                                                            </span>
                                                            <Switch checked={conn.enabled} onCheckedChange={() => toggleConnection(conn.id)} />
                                                        </div>
                                                        <button onClick={() => startEdit(conn)} aria-label={t('connections.edit.edit_aria', { name: conn.name })} className="p-2 text-muted-foreground hover:text-primary hover:bg-primary/10 rounded-lg transition-colors">
                                                            <Pencil className="w-4 h-4" />
                                                        </button>
                                                        <button onClick={() => removeConnection(conn.id)} className="p-2 text-muted-foreground hover:text-status-error hover:bg-status-error/10 rounded-lg transition-colors">
                                                            <Trash2 className="w-4 h-4" />
                                                        </button>
                                                    </div>
                                                </div>
                                            </div>
                                        ))}
                                    </div>
                                )}
                            </div>
                        </div>

                        <footer className="mt-20 pt-8 border-t border-border flex justify-between items-center text-xs font-black uppercase text-muted-foreground tracking-tighter">
                            <div>System Status: Nominal</div>
                            <div className="flex gap-6">
                                <a href="#" className="hover:text-primary transition-colors">Documentation</a>
                                <a href="#" className="hover:text-primary transition-colors">Security Audit</a>
                            </div>
                        </footer>
                    </div>
                </div>
            </main>
        </>
    );
}