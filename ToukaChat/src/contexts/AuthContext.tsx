'use client';

import { createContext, useContext, useEffect, useRef, useState, useCallback, ReactNode } from 'react';
import { useRouter } from 'next/navigation';
import { toast } from 'sonner';
import { authService, AuthRequestError, safeAuthError } from '../lib/api/authService';
import { User } from '../lib/types';
import {
  getActiveProxyUrl,
  fetchTouka,
  getAccessToken,
  setAccessToken as storeToken,
  clearAccessToken,
  setSessionExpiredHandler,
  markHasLoggedIn,
  getCredentialGeneration,
  captureRequestScope,
  subscribeCredentialScope,
  logoutTouka,
} from '../lib/connections';
import { logSSOTError } from '../lib/error_handler';
import i18n from '../lib/i18n';

interface AuthContextType {
  isAuthenticated: boolean;
  isLoading: boolean;
  accessToken: string | null;
  user: User | null;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refreshUser: () => Promise<void>;
}

interface AuthOperation {
  controller: AbortController;
  proxyUrl: string;
  epoch: number;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);
const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

function validProfile(value: unknown, principal: string | undefined): value is User {
  if (!record(value) || typeof value.id !== 'string' || !value.id ||
      typeof value.email !== 'string' || principal !== `access:${value.id}`) return false;
  if (['role', 'status'].some(key => value[key] !== undefined && typeof value[key] !== 'string')) return false;
  if (['display_name', 'avatar_url', 'bio'].some(key =>
    value[key] !== undefined && value[key] !== null && typeof value[key] !== 'string')) return false;
  if (['refresh_token_exp', 'last_active_at', 'created_at'].some(key =>
    value[key] !== undefined && value[key] !== null &&
    (typeof value[key] !== 'number' || !Number.isSafeInteger(value[key])))) return false;
  return value.owned_agents === undefined ||
    (Array.isArray(value.owned_agents) && value.owned_agents.every(id => typeof id === 'string'));
}

export const AuthProvider = ({ children }: { children: ReactNode }) => {
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [accessToken, setAccessToken] = useState<string | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const router = useRouter();
  const active = useRef<AuthOperation | null>(null);
  const ownCardWrite = useRef(false);

  const beginOperation = useCallback((): AuthOperation => {
    active.current?.controller.abort();
    const operation = {
      controller: new AbortController(),
      proxyUrl: getActiveProxyUrl(),
      epoch: getCredentialGeneration(),
    };
    active.current = operation;
    return operation;
  }, []);
  const current = useCallback((operation: AuthOperation) =>
    active.current === operation && !operation.controller.signal.aborted &&
    operation.proxyUrl === getActiveProxyUrl() && operation.epoch === getCredentialGeneration(), []);
  const requireCurrent = useCallback((operation: AuthOperation) => {
    if (!current(operation)) throw new DOMException('Authentication operation changed', 'AbortError');
  }, [current]);
  const writeCard = useCallback((operation: AuthOperation, write: () => void) => {
    requireCurrent(operation);
    ownCardWrite.current = true;
    try {
      write();
      operation.epoch = getCredentialGeneration();
    } finally {
      ownCardWrite.current = false;
    }
  }, [requireCurrent]);
  const clearView = useCallback(() => {
    setAccessToken(null);
    setUser(null);
    setIsAuthenticated(false);
  }, []);
  const fetchUserProfile = useCallback(async (operation: AuthOperation): Promise<User> => {
    try {
      requireCurrent(operation);
      const scope = captureRequestScope();
      if (!scope) throw new AuthRequestError('AUTH_PROFILE_FETCH_FAILED');
      const res = await fetchTouka(operation.proxyUrl, '/v1/auth/me', {
        signal: operation.controller.signal,
        nonStreaming: true,
        requestScope: scope,
        credentialEpoch: operation.epoch,
      });
      requireCurrent(operation);
      if (!res.ok) throw new AuthRequestError('AUTH_PROFILE_FETCH_FAILED');
      const body: unknown = await res.json();
      requireCurrent(operation);
      if (!validProfile(body, scope.authenticatedPrincipalId)) throw new AuthRequestError('AUTH_PROFILE_FETCH_FAILED');
      return body;
    } catch (error) {
      requireCurrent(operation);
      if (error instanceof DOMException && error.name === 'AbortError') throw error;
      throw new AuthRequestError('AUTH_PROFILE_FETCH_FAILED');
    }
  }, [requireCurrent]);

  useEffect(() => {
    setSessionExpiredHandler(() => {
      active.current?.controller.abort();
      active.current = null;
      clearAccessToken();
      clearView();
      setIsLoading(false);
      toast.warning(i18n.t('error.auth.session_expired'));
      router.push('/login');
    });
    return () => setSessionExpiredHandler(null);
  }, [router, clearView]);

  const authSettledRef = useRef(false);
  useEffect(() => {
    if (!authSettledRef.current) {
      authSettledRef.current = true;
      return;
    }
    window.dispatchEvent(new Event('touka_auth_changed'));
  }, [isAuthenticated]);

  useEffect(() => {
    let proxyUrl = getActiveProxyUrl();
    const invalidate = () => {
      if (ownCardWrite.current) return;
      active.current?.controller.abort();
      active.current = null;
      clearView();
      setIsLoading(false);
    };
    const changeOrigin = () => {
      const nextUrl = getActiveProxyUrl();
      if (nextUrl === proxyUrl) return;
      proxyUrl = nextUrl;
      invalidate();
      clearAccessToken();
    };
    const unsubscribe = subscribeCredentialScope(invalidate);
    window.addEventListener('touka_connections_updated', changeOrigin);
    const initAuth = async () => {
      const operation = beginOperation();
      try {
        const data = await authService.refresh({
          proxyUrl: operation.proxyUrl,
          signal: operation.controller.signal,
          credentialEpoch: operation.epoch,
        });
        requireCurrent(operation);
        writeCard(operation, () => storeToken(data.access_token));
        const profile = await fetchUserProfile(operation);
        requireCurrent(operation);
        setAccessToken(getAccessToken());
        setUser(profile);
        setIsAuthenticated(true);
        markHasLoggedIn();
      } catch {
        if (current(operation)) {
          writeCard(operation, clearAccessToken);
          clearView();
        }
      } finally {
        if (current(operation)) setIsLoading(false);
      }
    };
    void initAuth();
    return () => {
      active.current?.controller.abort();
      active.current = null;
      unsubscribe();
      window.removeEventListener('touka_connections_updated', changeOrigin);
    };
  }, [beginOperation, current, requireCurrent, writeCard, fetchUserProfile, clearView]);

  const login = async (email: string, password: string) => {
    const operation = beginOperation();
    writeCard(operation, () => clearAccessToken());
    setAccessToken(null);
    setUser(null);
    setIsAuthenticated(false);
    setIsLoading(false);
    try {
      const data = await authService.login(email, password, {
        proxyUrl: operation.proxyUrl,
        signal: operation.controller.signal,
        credentialEpoch: operation.epoch,
      });
      requireCurrent(operation);
      writeCard(operation, () => storeToken(data.access_token));
      const profile = await fetchUserProfile(operation);
      requireCurrent(operation);
      setAccessToken(getAccessToken());
      setUser(profile);
      setIsAuthenticated(true);
      markHasLoggedIn();
    } catch (error) {
      if (!current(operation)) throw new DOMException('Authentication operation changed', 'AbortError');
      writeCard(operation, () => clearAccessToken());
      clearView();
      if (error instanceof DOMException && error.name === 'AbortError') throw error;
      throw safeAuthError(error);
    }
  };

  const logout = async () => {
    const operation = beginOperation();
    // Capture the old card before invalidation; late completion cannot clear a new card.
    const pending = logoutTouka();
    writeCard(operation, () => clearAccessToken());
    setAccessToken(null);
    setUser(null);
    setIsAuthenticated(false);
    setIsLoading(false);
    try {
      await pending;
    } catch (error) {
      if (current(operation)) logSSOTError(safeAuthError(error), 'AuthContext:logout');
    } finally {
      if (current(operation)) router.push('/login');
    }
  };

  const refreshUser = async () => {
    if (!getAccessToken() || !isAuthenticated) return;
    const operation = beginOperation();
    try {
      const profile = await fetchUserProfile(operation);
      if (current(operation)) {
        setAccessToken(getAccessToken());
        setUser(profile);
      }
    } catch (error) {
      if (current(operation)) logSSOTError(safeAuthError(error), 'AuthContext:refreshUser');
    }
  };
  return <AuthContext.Provider value={{ isAuthenticated, isLoading, accessToken, user, login, logout, refreshUser }}>{children}</AuthContext.Provider>;
};

export const useAuth = () => {
  const context = useContext(AuthContext);
  if (context === undefined) throw new Error('useAuth must be used within an AuthProvider');
  return context;
};
