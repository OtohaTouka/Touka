import { useCallback, useEffect, useRef, useState, type SetStateAction } from 'react';
import { useTranslation } from 'react-i18next';
import { getActiveProxyUrl, fetchTouka } from '@/lib/connections';
import { errorMsg } from '@/lib/error_handler';
import { toast } from 'sonner';

// GET supplies write-only token placeholders; only the current form holds edits.
export type SnsBotsConfig = {
    discord: {id?: string, token: string, agent_id: string, enabled: boolean, session_mode?: string, is_multi_agent?: boolean}[];
    telegram: {id?: string, token: string, agent_id: string, enabled: boolean, session_mode?: string, is_multi_agent?: boolean}[];
};
const emptyConfig = (): SnsBotsConfig => ({ discord: [], telegram: [] });

function isBotsConfig(value: unknown): value is SnsBotsConfig {
    if (!value || typeof value !== 'object') return false;
    const config = value as Record<string, unknown>;
    return ['discord', 'telegram'].every(platform => {
        const bots = config[platform];
        return Array.isArray(bots) && bots.every(bot =>
            bot && typeof bot === 'object' && typeof bot.token === 'string' &&
            typeof bot.agent_id === 'string' && typeof bot.enabled === 'boolean' &&
            (bot.id === undefined || typeof bot.id === 'string')
        );
    });
}

function withoutTokenDrafts(draft: SnsBotsConfig, baseline: SnsBotsConfig): SnsBotsConfig {
    const scrub = (platform: keyof SnsBotsConfig) => draft[platform].map(bot => ({
        ...bot,
        token: bot.id ? baseline[platform].find(saved => saved.id === bot.id)?.token ?? '' : '',
    }));
    return { discord: scrub('discord'), telegram: scrub('telegram') };
}

export function useSnsBotSettings() {
    const { t } = useTranslation();
    const [snsBots, updateBots] = useState<SnsBotsConfig>(emptyConfig);
    const [loadState, setLoadState] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle');
    const [isSaving, setIsSaving] = useState(false);
    const [tokenDraftCleared, setTokenDraftCleared] = useState(false);
    const baseline = useRef<SnsBotsConfig>(emptyConfig());
    const loadedProxy = useRef<string | null>(null);
    const loading = useRef(false);
    const saving = useRef(false);
    const mounted = useRef(true);
    const generation = useRef(0);
    const loadAbort = useRef<AbortController | null>(null);

    const invalidatePendingLoads = useCallback(() => {
        generation.current++;
        loadAbort.current?.abort();
    }, []);

    useEffect(() => {
        mounted.current = true;
        return () => {
            mounted.current = false;
            invalidatePendingLoads();
            baseline.current = emptyConfig();
            loadedProxy.current = null;
        };
    }, [invalidatePendingLoads]);

    const fetchSnsBots = useCallback(async (): Promise<boolean> => {
        if (!mounted.current || saving.current) return false;
        loadAbort.current?.abort();
        const controller = new AbortController();
        loadAbort.current = controller;
        const operation = ++generation.current;
        const proxyUrl = getActiveProxyUrl();
        loadedProxy.current = null;
        loading.current = true;
        setLoadState('loading');
        updateBots(baseline.current);
        try {
            const response = await fetchTouka(proxyUrl, '/v1/bots', { signal: controller.signal });
            const data: unknown = response.ok ? await response.json() : null;
            if (!mounted.current || operation !== generation.current) return false;
            if (!isBotsConfig(data) || getActiveProxyUrl() !== proxyUrl) {
                setLoadState('error');
                return false;
            }
            baseline.current = data;
            loadedProxy.current = proxyUrl;
            updateBots(data);
            setLoadState('ready');
            return true;
        } catch {
            // Transport exceptions can contain credentials; report only safe UI state.
            if (mounted.current && operation === generation.current) setLoadState('error');
            return false;
        } finally {
            if (operation === generation.current) loading.current = false;
        }
    }, []);

    const setSnsBots = useCallback((value: SetStateAction<SnsBotsConfig>) => {
        if (!mounted.current || !loadedProxy.current || loading.current || saving.current) return;
        updateBots(value);
    }, []);

    const resetSnsDrafts = useCallback(() => {
        invalidatePendingLoads();
        loading.current = false;
        loadedProxy.current = null;
        if (mounted.current) {
            updateBots(baseline.current);
            setLoadState('idle');
            setTokenDraftCleared(false);
        }
    }, [invalidatePendingLoads]);

    const saveSnsBots = async (newBots: SnsBotsConfig): Promise<boolean> => {
        const proxyUrl = getActiveProxyUrl();
        if (!mounted.current || loading.current || saving.current || loadedProxy.current !== proxyUrl) return false;
        const invalid = [...newBots.discord, ...newBots.telegram].some(bot => !bot.token.trim() || !bot.agent_id.trim());
        if (invalid) {
            toast.error(t('hooks.use_sns_bot_settings.errors.bot_params_required'));
            return false;
        }
        const operation = ++generation.current;
        saving.current = true;
        setIsSaving(true);
        setTokenDraftCleared(false);
        let accepted = false;
        try {
            const response = await fetchTouka(proxyUrl, '/v1/bots', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(newBots),
            });
            accepted = response.ok;
        } catch {
            // Never log the exception or request body from a credential submission.
        } finally {
            saving.current = false;
            if (mounted.current) {
                setIsSaving(false);
                if (operation === generation.current) {
                    updateBots(current => withoutTokenDrafts(current, baseline.current));
                    setTokenDraftCleared(!accepted);
                }
            }
        }
        if (!mounted.current || operation !== generation.current) return false;
        if (accepted) {
            // POST does not echo credentials; only a fresh GET establishes the new baseline.
            await fetchSnsBots();
            if (mounted.current && operation + 1 === generation.current) toast.success(t('hooks.use_sns_bot_settings.bots_saved'));
        } else {
            toast.error(errorMsg('CONFIG_SAVE_FAILED'));
        }
        return accepted;
    };

    return { snsBots, setSnsBots, fetchSnsBots, saveSnsBots, resetSnsDrafts, loadState, isSaving, tokenDraftCleared };
}
