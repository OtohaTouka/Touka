/**
 * MCP 設定頁的隔離區塊：來源只能往嚴（後端說可以時才給「標成代理人建立」、改之前先確認），
 * 額外允許的資料夾只給第三方與 scaffold，存檔失敗顯示後端點名的第幾筆與原因。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const confirmToast = vi.fn();
vi.mock("@/hooks/useConfirmToast", () => ({
  useConfirmToast: () => ({ confirmToast }),
}));
vi.mock("@/lib/connections", () => ({
  fetchTouka: vi.fn(),
  getActiveProxyUrl: () => "http://x",
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

import { fetchTouka } from "@/lib/connections";
import { McpIsolationSection } from "../McpIsolationSection";

const mockFetch = fetchTouka as ReturnType<typeof vi.fn>;
const BOUND = "c".repeat(64);
const THIRD = {
  server: "files",
  class: "third_party",
  origin: "manual",
  record_state: "ok",
  sandbox_paths: [{ path: "/srv/docs", access: "ro" }],
  bound: BOUND,
  can_mark_agent: true,
};
const SELF = {
  ...THIRD,
  class: "self",
  origin: "agent",
  sandbox_paths: [],
  can_mark_agent: false,
};

const reply = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status });

function serve(view: object, put?: () => Response) {
  mockFetch.mockImplementation(
    async (_url: string, _path: string, init?: RequestInit) =>
      init?.method === "PUT" && put ? put() : reply(200, view),
  );
}

function putBodies() {
  return mockFetch.mock.calls
    .filter(([, , init]) => init?.method === "PUT")
    .map(([, , init]) => JSON.parse(init.body as string));
}

async function renderSection(view: object, put?: () => Response) {
  serve(view, put);
  const result = render(<McpIsolationSection serverKey="files" revision={1} />);
  await waitFor(() => expect(mockFetch).toHaveBeenCalled());
  return result;
}

describe("McpIsolationSection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetch.mockReset();
  });

  it("shows the source and asks before marking a third-party server as agent-built", async () => {
    await renderSection(THIRD, () => reply(200, SELF));
    expect(await screen.findByText("來源：我自己設定")).toBeInTheDocument();
    expect(screen.getByText("判定：第三方程式")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "標成代理人建立" }));
    expect(confirmToast).toHaveBeenCalledTimes(1);
    expect(confirmToast.mock.calls[0][0]).toMatch(/不能改回/);
    expect(putBodies()).toEqual([]);

    (confirmToast.mock.calls[0][1] as () => void)();
    await waitFor(() =>
      expect(putBodies()).toEqual([{ bound: BOUND, origin: "agent" }]),
    );
    // 改成自建後：不能再改來源，也不能有額外資料夾
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "標成代理人建立" }),
      ).toBeNull(),
    );
    expect(screen.queryByText("額外允許的資料夾")).toBeNull();
  });

  it("offers no way to change the source when the backend says it can't", async () => {
    await renderSection(SELF);
    expect(await screen.findByText("來源：代理人建立")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "標成代理人建立" })).toBeNull();
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(screen.queryByText("額外允許的資料夾")).toBeNull();
  });

  it("says when the server has no source record", async () => {
    await renderSection({ ...THIRD, origin: null, record_state: "stale" });
    expect(await screen.findByText("來源：未標示")).toBeInTheDocument();
    expect(screen.getByText(/程式改過/)).toBeInTheDocument();
  });

  it("edits the extra folders in order and saves them", async () => {
    await renderSection(THIRD, () => reply(200, THIRD));
    expect(await screen.findByDisplayValue("/srv/docs")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /新增資料夾/ }));
    const save = screen.getByRole("button", { name: "儲存資料夾" });
    expect(save).toBeDisabled(); // 第 2 筆還是空的：存檔的第幾筆要和畫面一致
    fireEvent.change(screen.getByLabelText("第 2 筆資料夾"), {
      target: { value: " /srv/repo " },
    });
    fireEvent.click(screen.getAllByRole("button", { name: "讀寫" })[1]);
    fireEvent.click(save);

    await waitFor(() =>
      expect(putBodies()).toEqual([
        {
          bound: BOUND,
          sandbox_paths: [
            { path: "/srv/docs", access: "ro" },
            { path: "/srv/repo", access: "rw" },
          ],
        },
      ]),
    );
  });

  it("shows which folder the backend refused and keeps what was typed", async () => {
    const refusal = {
      detail: {
        code: "VALIDATION_ERROR",
        message: "mcp_sandbox_path_refused:1:protected_location",
      },
    };
    await renderSection(THIRD, () => reply(422, refusal));
    await screen.findByDisplayValue("/srv/docs");
    fireEvent.click(screen.getByRole("button", { name: /新增資料夾/ }));
    fireEvent.change(screen.getByLabelText("第 2 筆資料夾"), {
      target: { value: "/app/config" },
    });
    fireEvent.click(screen.getByRole("button", { name: "儲存資料夾" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /第 2 筆：.*受保護/,
    );
    expect(screen.getByDisplayValue("/app/config")).toBeInTheDocument();
  });

  it("shows nothing for a remote server", async () => {
    const { container } = await renderSection({ ...THIRD, class: "remote" });
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });
});
