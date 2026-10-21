'use client';

import { ReactNode, useEffect } from 'react';
import { useRouter, usePathname } from 'next/navigation';
import { useAuth } from '../contexts/AuthContext';
import { Loader2 } from 'lucide-react';

const PUBLIC_ROUTES = ['/login', '/register'];

export const AuthGuard = ({ children }: { children: ReactNode }) => {
  const { isAuthenticated, isLoading } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (isLoading) return;

    const isPublicRoute = PUBLIC_ROUTES.includes(pathname);

    if (!isAuthenticated && !isPublicRoute) {
      router.push('/login');
    } else if (isAuthenticated && isPublicRoute) {
      router.push('/');
    }
  }, [isAuthenticated, isLoading, pathname, router]);

  if (isLoading) {
    return (
      <div 
        data-testid="auth-loading" 
        className="fixed inset-0 flex items-center justify-center bg-background z-50"
      >
        <Loader2 className="w-8 h-8 text-primary animate-spin" />
      </div>
    );
  }

  // Prevent rendering protected content briefly before redirect
  const isPublicRoute = PUBLIC_ROUTES.includes(pathname);
  if (!isAuthenticated && !isPublicRoute) {
    return null;
  }

  return <>{children}</>;
};
