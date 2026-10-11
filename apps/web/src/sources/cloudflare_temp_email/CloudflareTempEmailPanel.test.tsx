import { render, screen, fireEvent } from "@testing-library/react";
import { it, expect, vi } from "vitest";
import { CloudflareTempEmailPanel } from "./CloudflareTempEmailPanel";

const ITEM = {
  provider_key: "125",
  address: "tmp809a7130cd07@orangeai.cfd",
  status: "active",
  created_at: "2026-10-11T12:47:08Z",
  detail: { source_meta: "admin", mail_count: 3 },
};

const BASE = "/api/v1/sources/cloudflare_temp_email/c1";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status });
}

it("loads addresses and posts a create request with CSRF only", async () => {
  const request = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `${BASE}/items` && (init?.method ?? "GET") === "GET")
      return json([ITEM]);
    if (path === `${BASE}/items`) {
      return json({ ...ITEM, provider_key: "999", address: "tmpnew@orangeai.cfd" }, 201);
    }
    return json([]);
  });
  vi.stubGlobal("fetch", request);
  render(<CloudflareTempEmailPanel connectionId="c1" csrfToken="csrf" />);

  expect(await screen.findByText(ITEM.address)).toBeVisible();

  fireEvent.change(screen.getByLabelText("地址名称"), {
    target: { value: "tmpnew" },
  });
  fireEvent.click(screen.getByRole("button", { name: "创建地址" }));

  expect(await screen.findByText("tmpnew@orangeai.cfd")).toBeVisible();
  const init = request.mock.calls.find((call) => call[1]?.method === "POST")![1]!;
  expect(JSON.parse(init.body as string)).toEqual({
    name: "tmpnew",
    domain: null,
    enable_prefix: false,
  });
  expect(new Headers(init.headers).get("X-CSRF-Token")).toBe("csrf");
  expect(init.credentials).toBe("same-origin");
});

it("shows the address credential only inside an explicitly sensitive notice", async () => {
  const request = vi.fn(async (path: string) => {
    if (path === `${BASE}/items`) return json([ITEM]);
    if (path.endsWith("/password"))
      return json({
        provider_key: "125",
        sensitive: true,
        field: "jwt",
        value: "credential-value-abc",
      });
    return json([]);
  });
  vi.stubGlobal("fetch", request);
  render(<CloudflareTempEmailPanel connectionId="c1" csrfToken="csrf" />);

  await screen.findByText(ITEM.address);
  fireEvent.click(screen.getByRole("button", { name: `查看口令 ${ITEM.address}` }));

  expect(await screen.findByText("credential-value-abc")).toBeVisible();
  expect(screen.getByText(/敏感字段/)).toBeVisible();
});

it("reports mapped source errors and still sends CSRF on destructive actions", async () => {
  const request = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `${BASE}/items`) return json([ITEM]);
    if (path.includes("/items/125") && init?.method === "DELETE")
      return json({ detail: "SOURCE_AUTH" }, 502);
    return json([]);
  });
  vi.stubGlobal("fetch", request);
  render(<CloudflareTempEmailPanel connectionId="c1" csrfToken="csrf" />);

  await screen.findByText(ITEM.address);
  fireEvent.click(screen.getByRole("button", { name: `删除 ${ITEM.address}` }));

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "上游鉴权失败，请检查该连接的管理员口令。",
  );
  const del = request.mock.calls.find((call) => call[1]?.method === "DELETE")![1]!;
  expect(new Headers(del.headers).get("X-CSRF-Token")).toBe("csrf");
  // 失败时列表保持原样，不谎报删除成功
  expect(screen.getByText(ITEM.address)).toBeVisible();
});

it("loads and deletes upstream mails for an address", async () => {
  const request = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === `${BASE}/items`) return json([ITEM]);
    if (path.startsWith(`${BASE}/mails?`))
      return json({
        count: 1,
        items: [
          {
            provider_key: "9",
            address: ITEM.address,
            source: "smtp",
            message_id: "m-1",
            created_at: "2026-10-11T12:47:08Z",
            is_unread: true,
          },
        ],
      });
    if (path === `${BASE}/mails/9` && init?.method === "DELETE") return json(null, 204);
    return json([]);
  });
  vi.stubGlobal("fetch", request);
  render(<CloudflareTempEmailPanel connectionId="c1" csrfToken="csrf" />);

  await screen.findByText(ITEM.address);
  fireEvent.click(screen.getByRole("button", { name: `查看邮件 ${ITEM.address}` }));

  const removeMail = await screen.findByRole("button", { name: "删除邮件 9" });
  fireEvent.click(removeMail);

  expect(await screen.findByText(ITEM.address)).toBeVisible();
  const del = request.mock.calls.find(
    (call) => call[0] === `${BASE}/mails/9`,
  )![1]!;
  expect(new Headers(del.headers).get("X-CSRF-Token")).toBe("csrf");
});
