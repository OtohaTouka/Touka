import { expect, test, type BrowserContext, type Page } from "@playwright/test";
import http from "node:http";
import type { AddressInfo } from "node:net";
import { LOOPBACK } from "./lib/constants";
import { loginInBrowser, openChat, openSideMenu, shot } from "./lib/ui";

const REFRESH_COOKIE = "refresh_token";
const HEALTH = { status: "healthy", timestamp: "2026-11-05T00:00:00Z", permission_config_degraded: false, permission_config_degraded_reason: "" };
const LIST_PATH = "/v1/ui/connections";

interface SeenRequest {
  method: string;
  path: string;
  authorization: string | undefined;
  cookie: string | undefined;
  /** 預檢問能不能帶哪些標頭；要帶憑證的話這裡會有 authorization。 */
  preflightHeaders: string | undefined;
}

interface FakeBackend {
  origin: string;
  seen: SeenRequest[];
  close: () => Promise<void>;
}

/**
 * 當新後端。和真後端同一台主機、只差埠號，正好是 cookie 會帶過來的情況。
 * CORS 全部放行（含憑證）：不放行的話，就算程式想帶憑證，瀏覽器也不會真的送出，測試看不出來。
 */
async function startFakeBackend(): Promise<FakeBackend> {
  const seen: SeenRequest[] = [];
  const server = http.createServer((req, res) => {
    seen.push({
      method: req.method ?? "",
      path: (req.url ?? "").split("?")[0],
      authorization: req.headers.authorization,
      cookie: req.headers.cookie,
      preflightHeaders: req.headers["access-control-request-headers"],
    });
    res.setHeader("Access-Control-Allow-Origin", req.headers.origin ?? "*");
    res.setHeader("Access-Control-Allow-Credentials", "true");
    res.setHeader("Access-Control-Allow-Methods", "GET,POST,PUT,PATCH,DELETE,OPTIONS");
    if (req.headers["access-control-request-headers"]) {
      res.setHeader("Access-Control-Allow-Headers", req.headers["access-control-request-headers"]);
    }
    if (req.method === "OPTIONS") {
      res.writeHead(204).end();
      return;
    }
    res.setHeader("Content-Type", "application/json");
    const isHealth = (req.url ?? "").startsWith("/v1/health");
    res.writeHead(isHealth ? 200 : 401).end(JSON.stringify(isHealth ? HEALTH : { detail: "no session" }));
  });
  await new Promise<void>((resolve) => server.listen(0, LOOPBACK, resolve));
  const { port } = server.address() as AddressInfo;
  return {
    origin: `http://${LOOPBACK}:${port}`,
    seen,
    close: () => new Promise((resolve) => {
      server.closeAllConnections();
      server.close(() => resolve());
    }),
  };
}

async function openConnectionsTab(page: Page): Promise<void> {
  await openSideMenu(page);
  await page.getByRole("menuitem", { name: "設定", exact: true }).click();
  await page.getByText("模型與供應商", { exact: true }).first().click();
  await page.getByText("連接端點", { exact: true }).first().click();
  await page.getByRole("heading", { name: "連線管理" }).waitFor();
}

/** 新增表單的提供者預設就是 Proxy。 */
async function addProxy(page: Page, url: string): Promise<void> {
  await page.getByPlaceholder("http://localhost:8000").fill(url);
  await page.getByRole("button", { name: "新增連線" }).click();
}

async function refreshCookie(context: BrowserContext): Promise<string | undefined> {
  return (await context.cookies()).find((cookie) => cookie.name === REFRESH_COOKIE)?.value;
}

let backend: FakeBackend;

test.beforeEach(async () => {
  backend = await startFakeBackend();
});

test.afterEach(async () => {
  await backend.close();
});

test("換後端：確認後兩個分頁都回登入頁，新後端收不到憑證、cookie 與清單", async ({ page, context }, info) => {
  await loginInBrowser(page);
  const otherTab = await context.newPage();
  await openChat(otherTab);
  expect(await refreshCookie(context)).toBeTruthy();

  await openConnectionsTab(page);
  await addProxy(page, backend.origin);
  await expect(page.getByText(/要把後端換成/)).toContainText(backend.origin);
  await shot(page, info, "confirm");
  await page.getByRole("button", { name: "換後端並登出" }).click();

  for (const tab of [page, otherTab]) {
    await tab.waitForURL(/\/login/);
    await expect(tab.getByText(`連線到：${backend.origin}`)).toBeVisible();
  }
  await shot(page, info, "login-page");

  // 登入頁的系統狀態檢查會帶 credentials 打新後端：真的有 cookie 就會出現在這裡。
  await expect.poll(() => backend.seen.filter((request) => request.method === "GET" && request.path === "/v1/health").length)
    .toBeGreaterThan(1);
  expect(await refreshCookie(context)).toBeUndefined();
  expect(backend.seen.filter((request) => request.authorization !== undefined)).toEqual([]);
  expect(backend.seen.filter((request) => /authorization/i.test(request.preflightHeaders ?? ""))).toEqual([]);
  expect(backend.seen.filter((request) => request.cookie?.includes(REFRESH_COOKIE))).toEqual([]);
  expect(backend.seen.filter((request) => request.path === LIST_PATH)).toEqual([]);
  await otherTab.close();
});

test("換後端：新網址連不上就不換，人還在、可以重試", async ({ page, context }, info) => {
  await backend.close();
  await loginInBrowser(page);
  await openConnectionsTab(page);
  const cookieBefore = await refreshCookie(context);
  const stored = () => page.evaluate(() => window.localStorage.getItem("touka_connections"));
  const listBefore = await stored();

  await addProxy(page, backend.origin);
  await page.getByRole("button", { name: "換後端並登出" }).click();

  await expect(page.getByText(/連不上 .*網址沒有變更/)).toContainText(backend.origin);
  await shot(page, info, "unreachable");
  expect(page.url()).not.toContain("/login");
  await expect(page.getByRole("heading", { name: "連線管理" })).toBeVisible();
  expect(await refreshCookie(context)).toBe(cookieBefore);
  expect(await stored()).toBe(listBefore);
  // 按下後提示還有 0.2 秒退場動畫；這段時間再跳一個，畫面上會有兩顆一樣的按鈕
  await expect(page.getByRole("button", { name: "換後端並登出" })).toHaveCount(0);

  // 換一個連得上的網址，同一個畫面可以重試。
  backend = await startFakeBackend();
  await addProxy(page, backend.origin);
  await page.getByRole("button", { name: "換後端並登出" }).click();
  await page.waitForURL(/\/login/);
  await expect(page.getByText(`連線到：${backend.origin}`)).toBeVisible();
});
