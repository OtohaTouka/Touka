import { describe, it, expect, beforeEach } from 'vitest';
import { avatarSrc } from '@/lib/connections';

// avatarSrc (ADR-0106): relative avatar paths must be resolved against the proxy
// origin (uploaded avatars are served by the backend, not the frontend origin);
// absolute external URLs pass through unchanged.
describe('avatarSrc (ADR-0106)', () => {
  beforeEach(() => {
    localStorage.clear(); // no active connection → getActiveProxyUrl falls back to CONFIG.PROXY_URL
  });

  it('empty / null → empty string', () => {
    expect(avatarSrc("")).toBe("");
    expect(avatarSrc(null)).toBe("");
    expect(avatarSrc(undefined)).toBe("");
  });

  it('absolute http(s)/data URLs pass through unchanged', () => {
    expect(avatarSrc("https://cdn.example.com/a.png")).toBe("https://cdn.example.com/a.png");
    expect(avatarSrc("http://x/y.jpg")).toBe("http://x/y.jpg");
    expect(avatarSrc("data:image/png;base64,AAAA")).toBe("data:image/png;base64,AAAA");
  });

  it('relative proxy path is prefixed with the active proxy origin', () => {
    localStorage.setItem(
      "touka_connections",
      JSON.stringify([{ id: "proxy", provider: "proxy", enabled: true, url: "http://localhost:8000" }]),
    );
    expect(avatarSrc("/v1/users/u1/avatar?v=123")).toBe("http://localhost:8000/v1/users/u1/avatar?v=123");
  });
});
