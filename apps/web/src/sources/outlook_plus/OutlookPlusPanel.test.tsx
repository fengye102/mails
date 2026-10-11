import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { it, expect, vi } from "vitest";
import { OutlookPlusPanel } from "./OutlookPlusPanel";

const ITEMS = [
  {
    provider_key: "user1@outlook.com",
    address: "user1@outlook.com",
    status: "active",
    created_at: "2026-07-09T06:32:10Z",
    detail: { id: 1, group_name: "默认分组" },
  },
  {
    provider_key: "user2@outlook.com",
    address: "user2@outlook.com",
    status: "inactive",
    created_at: null,
    detail: { id: 2 },
  },
];

it("renders pool accounts from the source endpoint", async () => {
  const request = vi.fn(
    async (_path: string, _init?: RequestInit) =>
      new Response(JSON.stringify(ITEMS), { status: 200 }),
  );
  vi.stubGlobal("fetch", request);
  render(<OutlookPlusPanel connectionId="c1" csrfToken="csrf" />);
  expect(await screen.findByText("user1@outlook.com")).toBeVisible();
  expect(screen.getByText("user2@outlook.com")).toBeVisible();
  expect(request.mock.calls[0][0]).toBe("/api/v1/sources/outlook_plus/c1/items");
});

it("submits the account string once, with csrf, and clears it", async () => {
  const request = vi.fn(async (_path: string, init?: RequestInit) =>
    init?.method === "POST"
      ? new Response(
          JSON.stringify({
            provider_key: "new@outlook.com",
            address: "new@outlook.com",
            status: "active",
            created_at: null,
            detail: { id: 9 },
          }),
          { status: 201 },
        )
      : new Response("[]", { status: 200 }),
  );
  vi.stubGlobal("fetch", request);
  render(<OutlookPlusPanel connectionId="c1" csrfToken="csrf" />);
  await screen.findByText("该连接下暂无账号。");
  fireEvent.change(screen.getByLabelText(/账号串/), {
    target: { value: "new@outlook.com----pw----cid----rt" },
  });
  fireEvent.click(screen.getByRole("button", { name: "导入账号" }));
  expect(await screen.findByText("new@outlook.com")).toBeVisible();
  const post = request.mock.calls.find((call) => call[1]?.method === "POST")!;
  const init = post[1]!;
  expect(JSON.parse(init.body as string)).toEqual({
    account_string: "new@outlook.com----pw----cid----rt",
    group_id: 1,
  });
  expect(new Headers(init.headers).get("X-CSRF-Token")).toBe("csrf");
  expect(init.credentials).toBe("same-origin");
  expect(screen.getByLabelText(/账号串/)).toHaveValue("");
  expect(document.body).not.toHaveTextContent("----pw----");
});

it("deletes an account with csrf and removes the row", async () => {
  const request = vi.fn(async (_path: string, init?: RequestInit) =>
    init?.method === "DELETE"
      ? new Response(null, { status: 204 })
      : new Response(JSON.stringify(ITEMS), { status: 200 }),
  );
  vi.stubGlobal("fetch", request);
  render(<OutlookPlusPanel connectionId="c1" csrfToken="csrf" />);
  await screen.findByText("user1@outlook.com");
  fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
  const call = request.mock.calls.find((entry) => entry[1]?.method === "DELETE")!;
  expect(call[0]).toBe("/api/v1/sources/outlook_plus/c1/items/user1%40outlook.com");
  expect(new Headers(call[1]!.headers).get("X-CSRF-Token")).toBe("csrf");
  await waitFor(() => expect(screen.queryByText("user1@outlook.com")).toBeNull());
});

it("refreshes a token through the account endpoint", async () => {
  const request = vi.fn(async (_path: string, init?: RequestInit) =>
    init?.method === "POST"
      ? new Response(JSON.stringify({ account_id: 1, status: "refreshed" }), { status: 200 })
      : new Response(JSON.stringify(ITEMS), { status: 200 }),
  );
  vi.stubGlobal("fetch", request);
  render(<OutlookPlusPanel connectionId="c1" csrfToken="csrf" />);
  await screen.findByText("user1@outlook.com");
  fireEvent.click(screen.getAllByRole("button", { name: "刷新令牌" })[0]);
  const call = request.mock.calls.find((entry) => entry[1]?.method === "POST")!;
  expect(call[0]).toBe("/api/v1/sources/outlook_plus/c1/accounts/1/refresh");
  expect(new Headers(call[1]!.headers).get("X-CSRF-Token")).toBe("csrf");
});

it("exposes the export download link", async () => {
  const request = vi.fn(
    async () => new Response(JSON.stringify(ITEMS), { status: 200 }),
  );
  vi.stubGlobal("fetch", request);
  render(<OutlookPlusPanel connectionId="c1" csrfToken="csrf" />);
  await screen.findByText("user1@outlook.com");
  expect(screen.getByRole("link", { name: "导出账号清单" })).toHaveAttribute(
    "href",
    "/api/v1/sources/outlook_plus/c1/export",
  );
});

it("does not echo the account secret when import fails", async () => {
  const request = vi.fn(async (_path: string, init?: RequestInit) =>
    init?.method === "POST"
      ? new Response(JSON.stringify({ detail: "SOURCE_REJECTED" }), { status: 502 })
      : new Response("[]", { status: 200 }),
  );
  vi.stubGlobal("fetch", request);
  render(<OutlookPlusPanel connectionId="c1" csrfToken="csrf" />);
  await screen.findByText("该连接下暂无账号。");
  fireEvent.change(screen.getByLabelText(/账号串/), {
    target: { value: "secret@x.com----topsecret----cid----rt" },
  });
  fireEvent.click(screen.getByRole("button", { name: "导入账号" }));
  expect(await screen.findByRole("alert")).toBeVisible();
  expect(document.body).not.toHaveTextContent("topsecret");
  expect(screen.getByLabelText(/账号串/)).toHaveValue("");
});
