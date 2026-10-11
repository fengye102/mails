import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SourceManagementPage } from "./SourceManagementPage";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("来源管理入口", () => {
  it("默认展示 iCloud HME 面板，并可切换到临时邮箱", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        String(url).includes("/connections") ? jsonResponse([]) : jsonResponse({}),
      ),
    );
    render(<SourceManagementPage csrfToken="csrf" />);

    expect(await screen.findByRole("tab", { name: "iCloud HME" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    fireEvent.click(screen.getByRole("tab", { name: "临时邮箱" }));
    expect(screen.getByRole("tab", { name: "临时邮箱" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("没有 Outlook 连接时给出明确提示而不是空白", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        String(url).includes("/connections") ? jsonResponse([]) : jsonResponse({}),
      ),
    );
    render(<SourceManagementPage csrfToken="csrf" />);

    fireEvent.click(screen.getByRole("tab", { name: "Outlook 邮箱池" }));

    expect(await screen.findByRole("status")).toHaveTextContent("还没有 Outlook 邮箱池连接");
  });

  it("有 Outlook 连接时渲染对应面板", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        const path = String(url);
        if (path.includes("/connections"))
          return jsonResponse([
            {
              id: "conn-oep",
              kind: "outlook_plus",
              name: "OEP",
              base_url: "https://plus.example.test",
              status: "active",
              last_error: null,
              last_checked_at: null,
            },
          ]);
        if (path.includes("/items")) return jsonResponse([]);
        return jsonResponse({});
      }),
    );
    render(<SourceManagementPage csrfToken="csrf" />);

    fireEvent.click(screen.getByRole("tab", { name: "Outlook 邮箱池" }));

    await waitFor(() =>
      expect(screen.queryByRole("status")).not.toBeInTheDocument(),
    );
  });

  it("连接列表加载失败时给出错误提示", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "UPSTREAM_TIMEOUT" }, 502)),
    );
    render(<SourceManagementPage csrfToken="csrf" />);

    const alerts = await screen.findAllByRole("alert");
    const texts = alerts.map((item) => item.textContent ?? "");
    expect(texts.some((text) => text.includes("上游响应超时，请重试。"))).toBe(true);
  });
});
