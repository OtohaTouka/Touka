'use client';

import React, { useState } from 'react';
import { useRouter } from 'next/navigation';
import { authService } from '../../lib/api/authService';
import { Card, CardHeader, CardContent } from '../../components/ui/card';
import { Input } from '../../components/ui/input';
import { Button } from '../../components/ui/button';

export default function RegisterPage() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const router = useRouter();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setIsLoading(true);
    
    try {
      await authService.register(email, password);
      router.push('/login');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Registration failed');
    } finally {
      setIsLoading(false);
    }
  };

  return (
    // WCAG（A5 axe）：頁面需 main 地標＋skip link 錨點
    <main id="main-content" className="flex items-center justify-center min-h-screen bg-background">
      <Card className="w-full max-w-md">
        <CardHeader>
          {/* WCAG 2.4.6：頁面需有真實標題元素（CardTitle 是 div）；樣式比照 CardTitle 保持視覺不變 */}
          <h1 className="text-2xl text-center leading-none font-semibold">Register for Touka</h1>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-2">
              {/* WCAG 3.3.2：程式化標籤（sr-only 保持視覺不變，placeholder 仍為可視提示） */}
              <label htmlFor="register-email" className="sr-only">Email</label>
              <Input
                id="register-email"
                type="email"
                placeholder="Email"
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
              />
            </div>
            <div className="space-y-2">
              <label htmlFor="register-password" className="sr-only">Password</label>
              <Input
                id="register-password"
                type="password"
                placeholder="Password"
                autoComplete="new-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
              />
            </div>
            {error && <div className="text-status-error text-sm text-center">{error}</div>}
            <Button type="submit" className="w-full" disabled={isLoading}>
              {isLoading ? 'Registering...' : 'Register'}
            </Button>
            <div className="text-center text-sm mt-4 text-muted-foreground">
              Already have an account?{' '}
              {/* WCAG 2.1.1（A2）：span onClick 鍵盤不可達 → 真 button */}
              <button
                type="button"
                className="text-primary cursor-pointer hover:underline"
                onClick={() => router.push('/login')}
              >
                Login
              </button>
            </div>
          </form>
        </CardContent>
      </Card>
    </main>
  );
}
