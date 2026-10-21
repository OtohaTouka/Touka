'use client';

import React, { useRef, useState } from 'react';
import { logSSOTError } from '../../lib/error_handler';
import { useRouter } from 'next/navigation';
import { useAuth } from '../../contexts/AuthContext';
import { Card, CardHeader, CardContent } from '../../components/ui/card';
import { Input } from '../../components/ui/input';
import { Button } from '../../components/ui/button';
import { safeAuthError } from '../../lib/api/authService';

export default function LoginPage() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [pending, setPending] = useState(false);
  const submitting = useRef(false);
  const { login } = useAuth();
  const router = useRouter();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (submitting.current) return;
    submitting.current = true;
    setPending(true);
    setError('');
    
    try {
      await login(email, password);
      router.push('/');
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return;
      const safe = safeAuthError(err);
      logSSOTError(safe, "LoginPage:login");
      setError(safe.message);
    } finally {
      submitting.current = false;
      setPending(false);
    }
  };

  return (
    // WCAG（A5 axe）：頁面需 main 地標＋skip link 錨點
    <main id="main-content" className="flex items-center justify-center min-h-screen bg-background">
      <Card className="w-full max-w-md">
        <CardHeader>
          {/* WCAG 2.4.6：頁面需有真實標題元素（CardTitle 是 div）；樣式比照 CardTitle 保持視覺不變 */}
          <h1 className="text-2xl text-center leading-none font-semibold">Login to Touka</h1>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-2">
              {/* WCAG 3.3.2：程式化標籤（sr-only 保持視覺不變，placeholder 仍為可視提示） */}
              <label htmlFor="login-email" className="sr-only">Email</label>
              <Input
                id="login-email"
                type="email"
                placeholder="Email"
                autoComplete="username"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
              />
            </div>
            <div className="space-y-2">
              <label htmlFor="login-password" className="sr-only">Password</label>
              <Input
                id="login-password"
                type="password"
                placeholder="Password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
              />
            </div>
            {error && <div className="text-status-error text-sm text-center">{error}</div>}
            <Button type="submit" className="w-full" disabled={pending}>
              Login
            </Button>
            <div className="text-center text-sm mt-4 text-muted-foreground">
              Don&apos;t have an account?{' '}
              {/* WCAG 2.1.1（A2）：span onClick 鍵盤不可達 → 真 button */}
              <button
                type="button"
                className="text-primary cursor-pointer hover:underline"
                onClick={() => router.push('/register')}
              >
                Register
              </button>
            </div>
          </form>
        </CardContent>
      </Card>
    </main>
  );
}
