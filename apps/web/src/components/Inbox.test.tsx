import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { it, expect, vi } from "vitest";
import { Inbox } from "./Inbox";
it("shows code only for an unambiguous message and copies it", async () => {
  const copy = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: copy },
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async (path: string) =>
        new Response(
          JSON.stringify(
            path.includes("/mailboxes")
              ? []
              : {
                  items: [
                    { id: "m1", subject: "验证", verification_code: "123456" },
                    { id: "m2", subject: "账单", verification_code: null },
                  ],
                  total: 2,
                },
          ),
        ),
    ),
  );
  render(<Inbox />);
  expect(await screen.findByText("123456")).toBeVisible();
  expect(screen.getAllByRole("button", { name: "复制验证码" })).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "复制验证码" }));
  expect(await screen.findByText("验证码已复制")).toBeVisible();
  expect(copy).toHaveBeenCalledWith("123456");
});
it("filters using API field names and renders detail as literal text at mobile width", async () => {
  Object.defineProperty(window, "innerWidth", {
    configurable: true,
    value: 375,
  });
  const request = vi.fn(
    async (path: string) =>
      new Response(
        JSON.stringify(
          path.includes("/mailboxes")
            ? [{ id: "b1", address: "one@example.com" }]
            : path.includes("/messages/m1")
              ? {
                  id: "m1",
                  subject: "验证",
                  body_text: "<img src=x onerror=alert(1)>",
                  verification_code: null,
                }
              : {
                  items: [
                    { id: "m1", subject: "验证", verification_code: null },
                  ],
                  total: 1,
                },
        ),
      ),
  );
  vi.stubGlobal("fetch", request);
  render(<Inbox />);
  await screen.findByRole("option", { name: "one@example.com" });
  fireEvent.change(screen.getByLabelText("邮箱"), { target: { value: "b1" } });
  fireEvent.change(screen.getByLabelText("发件人"), {
    target: { value: "sender" },
  });
  fireEvent.change(screen.getByLabelText("主题"), {
    target: { value: "verify" },
  });
  fireEvent.change(screen.getByLabelText("开始时间"), {
    target: { value: "2026-10-01T10:00" },
  });
  fireEvent.change(screen.getByLabelText("结束时间（不含）"), {
    target: { value: "2026-10-02T10:00" },
  });
  fireEvent.click(screen.getByRole("button", { name: "筛选邮件" }));
  await screen.findByRole("button", { name: "验证" });
  const url = request.mock.calls
    .map((c) => c[0])
    .find((p) => p.includes("sender=sender"))!;
  expect(url).toContain("mailbox_id=b1");
  expect(url).toContain("subject=verify");
  expect(url).toContain("from=");
  expect(url).toContain("to=");
  fireEvent.click(screen.getByRole("button", { name: "验证" }));
  expect(await screen.findByText("<img src=x onerror=alert(1)>")).toBeVisible();
  expect(document.querySelector("img")).toBeNull();
  await waitFor(() =>
    expect(screen.getByRole("heading", { name: "邮件详情" })).toHaveFocus(),
  );
});
it("explains empty results", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async (path: string) =>
        new Response(
          JSON.stringify(
            path.includes("/mailboxes") ? [] : { items: [], total: 0 },
          ),
        ),
    ),
  );
  render(<Inbox />);
  expect(
    await screen.findByText(
      "没有匹配的邮件。调整筛选条件，或到邮箱页执行同步。",
    ),
  ).toBeVisible();
});

it("searches across message text and filters by source", async () => {
  const request = vi.fn(async (path: string) =>
    new Response(
      JSON.stringify(
        path.includes("/mailboxes")
          ? []
          : {
              items: [
                {
                  id: "m1",
                  subject: "验证",
                  sender: "sender@example.com",
                  snippet: "验证码",
                  source_kind: "cymail",
                  identity_status: "same_rfc_id",
                  verification_code: null,
                },
              ],
              total: 1,
            },
      ),
    ),
  );
  vi.stubGlobal("fetch", request);
  render(<Inbox />);

  const search = await screen.findByLabelText("全文搜索");
  fireEvent.change(search, { target: { value: "验证码" } });
  fireEvent.change(screen.getByLabelText("来源类型"), {
    target: { value: "cymail" },
  });
  fireEvent.click(screen.getByRole("button", { name: "筛选邮件" }));

  await screen.findByRole("button", { name: "验证" });
  const url = request.mock.calls
    .map((call) => call[0])
    .find((path) => path.includes("query=%E9%AA%8C%E8%AF%81%E7%A0%81"))!;
  expect(url).toContain("source_kind=cymail");
  expect(screen.getByText("CYMail")).toBeVisible();
  expect(
    screen.getByText((text) => text.includes("RFC Message-ID 相同（未合并）")),
  ).toBeVisible();
});

it("reads mail straight from an upstream source without registering a mailbox", async () => {
  const copy = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: copy },
  });
  const request = vi.fn(async (path: string) => {
    if (path.includes("/live-mail/detail"))
      return new Response(
        JSON.stringify({
          id: "graph:9",
          subject: "验证码",
          sender: "no-reply@example.com",
          recipient: "user@outlook.com",
          received_at: "2026-07-09T06:32:10+00:00",
          verification_code: "654321",
          body_text: "验证码 654321",
          link: "https://openxlab.org.cn/activate?token=abc",
        }),
      );
    if (path.includes("/live-mail?"))
      return new Response(
        JSON.stringify({
          total: 1,
          items: [
            {
              provider_key: "graph:9",
              source_kind: "outlook_plus",
              sender: "no-reply@example.com",
              subject: "验证码",
              snippet: "请查看正文",
              received_at: "2026-07-09T06:32:10+00:00",
              verification_code: "654321",
            },
          ],
        }),
      );
    if (path.includes("/sources/outlook_plus/"))
      return new Response(
        JSON.stringify([
          { provider_key: "user@outlook.com", address: "user@outlook.com" },
        ]),
      );
    if (path.includes("/connections"))
      return new Response(
        JSON.stringify([
          {
            id: "c1",
            kind: "outlook_plus",
            name: "Outlook 池",
            base_url: "https://mail.example",
            status: "active",
            last_error: null,
            last_checked_at: null,
          },
        ]),
      );
    if (path.includes("/mailboxes")) return new Response(JSON.stringify([]));
    return new Response(JSON.stringify({ items: [], total: 0 }));
  });
  vi.stubGlobal("fetch", request);
  render(<Inbox />);

  fireEvent.click(await screen.findByRole("tab", { name: "上游实时" }));
  await waitFor(() =>
    expect(
      request.mock.calls.some((call) =>
        String(call[0]).includes("/sources/outlook_plus/c1/items"),
      ),
    ).toBe(true),
  );
  fireEvent.change(screen.getByLabelText("邮箱地址"), {
    target: { value: "user@outlook.com" },
  });
  fireEvent.click(screen.getByRole("button", { name: "读取邮件" }));

  expect(await screen.findByText("654321")).toBeVisible();
  const listUrl = request.mock.calls
    .map((call) => String(call[0]))
    .find((path) => path.includes("/live-mail?"))!;
  expect(listUrl).toContain("address=user%40outlook.com");
  expect(listUrl).toContain("limit=50");

  fireEvent.click(screen.getByRole("button", { name: "验证码" }));
  expect(await screen.findByText("验证码 654321")).toBeVisible();
  expect(
    request.mock.calls.some((call) =>
      String(call[0]).includes("/live-mail/detail"),
    ),
  ).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "复制验证码" }));
  expect(await screen.findByText("验证码已复制")).toBeVisible();
  expect(copy).toHaveBeenCalledWith("654321");
  // 链接型激活邮件：纯文本阅读策略下把链接单独暴露出来
  const link = screen.getByRole("link", {
    name: "https://openxlab.org.cn/activate?token=abc",
  });
  expect(link).toHaveAttribute("target", "_blank");
  fireEvent.click(screen.getByRole("button", { name: "复制链接" }));
  await waitFor(() =>
    expect(copy).toHaveBeenCalledWith(
      "https://openxlab.org.cn/activate?token=abc",
    ),
  );
});

it("asks for a source connection before reading upstream mail", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async (path: string) =>
        new Response(
          JSON.stringify(
            path.includes("/connections") || path.includes("/mailboxes")
              ? []
              : { items: [], total: 0 },
          ),
        ),
    ),
  );
  render(<Inbox />);
  fireEvent.click(await screen.findByRole("tab", { name: "上游实时" }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "读取邮件" })).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "读取邮件" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "请先选择一个来源连接。",
  );
});
