import { expect, test } from "@playwright/test";

const CHAT_ORIGIN = "https://chat.example.test";
const PROXY_ORIGIN = "https://proxy.example.test";
const STATE_CANARY = "state-browser-canary";
const CODE_CANARY = "code-browser-canary";

test("callback只留下opaque outcome並清除Touka可控制的secret surface", async ({
  context,
  page,
}) => {
  const consoleLines: string[] = [];
  context.on("page", (openedPage) => {
    openedPage.on("console", (message) => consoleLines.push(message.text()));
  });
  page.on("console", (message) => consoleLines.push(message.text()));
  await context.addInitScript(() => {
    window.close = () => {
      Object.defineProperty(window, "__toukaCloseRequested", {
        value: true,
        configurable: true,
      });
    };
  });
  await context.route(`${CHAT_ORIGIN}/**`, async (route) => {
    await route.fulfill({
      contentType: "text/html",
      body: "<!doctype html><meta charset=utf-8><title>OAuth opener</title>",
    });
  });
  await context.route(`${PROXY_ORIGIN}/**`, async (route) => {
    await route.fulfill({
      contentType: "text/html",
      headers: {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy":
          "default-src 'none'; base-uri 'none'; form-action 'none'; script-src 'unsafe-inline'; frame-ancestors 'none'",
      },
      body:
        "<!doctype html><meta charset=utf-8><title>OAuth complete</title>" +
        "<script>history.replaceState(null,'',location.pathname);" +
        `if(window.opener){window.opener.postMessage({type:'touka:mcp-oauth',operationId:'op-browser-safe',status:'connected'},'${CHAT_ORIGIN}');}` +
        "window.close();</script>",
    });
  });

  await page.goto(`${CHAT_ORIGIN}/opener`);
  await page.evaluate(() => {
    Object.defineProperty(window, "__toukaMessages", {
      value: [] as unknown[],
      configurable: true,
    });
    window.addEventListener("message", (event) => {
      const popup = Reflect.get(window, "__toukaPopup");
      Reflect.get(window, "__toukaMessages").push({
        data: event.data,
        origin: event.origin,
        sourceMatches: event.source === popup,
      });
    });
  });
  const popupPromise = page.waitForEvent("popup");
  await page.evaluate(
    ({ proxyOrigin, state, code }) => {
      Reflect.set(
        window,
        "__toukaPopup",
        window.open(
          `${proxyOrigin}/mcp/oauth/callback?state=${state}&code=${code}`,
          "touka-mcp-oauth",
        ),
      );
    },
    { proxyOrigin: PROXY_ORIGIN, state: STATE_CANARY, code: CODE_CANARY },
  );
  const popup = await popupPromise;
  await expect
    .poll(async () => page.evaluate(() => Reflect.get(window, "__toukaMessages").length))
    .toBe(1);

  expect(popup.url()).toBe(`${PROXY_ORIGIN}/mcp/oauth/callback`);
  expect(popup.url()).not.toContain(STATE_CANARY);
  expect(popup.url()).not.toContain(CODE_CANARY);
  expect(
    await popup.evaluate(() => ({
      local: Object.keys(window.localStorage),
      session: Object.keys(window.sessionStorage),
      closeRequested: Reflect.get(window, "__toukaCloseRequested") === true,
    })),
  ).toEqual({ local: [], session: [], closeRequested: true });
  const messages = await page.evaluate(() => Reflect.get(window, "__toukaMessages"));
  expect(messages).toEqual([
    {
      data: {
        type: "touka:mcp-oauth",
        operationId: "op-browser-safe",
        status: "connected",
      },
      origin: PROXY_ORIGIN,
      sourceMatches: true,
    },
  ]);
  const controlledSurfaces = JSON.stringify({ messages, consoleLines });
  expect(controlledSurfaces).not.toContain(STATE_CANARY);
  expect(controlledSurfaces).not.toContain(CODE_CANARY);
});
