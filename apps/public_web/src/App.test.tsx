import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { App } from "./App";

const mailbox = {
  id: "box-1",
  address: "demo@example.com",
  status: "active",
  expires_at: "2099-01-01T00:00:00Z",
  remaining_seconds: 3600,
};

it("creates a mailbox and reads its messages without using localStorage", async () => {
  const request = vi.fn(async (path: string) => {
    if (path.endsWith("/messages"))
      return new Response(
        JSON.stringify({
          items: [
            {
              id: "message-1",
              sender: "sender@example.com",
              subject: "Your verification code",
              snippet: "Code: 482901",
              received_at: "2099-01-01T00:00:00Z",
              verification_code: "482901",
            },
          ],
          total: 1,
        }),
      );
    if (path.includes("/messages/message-1"))
      return new Response(
        JSON.stringify({
          id: "message-1",
          sender: "sender@example.com",
          recipient: "demo@example.com",
          subject: "Your verification code",
          body_text: "Code: 482901",
          received_at: "2099-01-01T00:00:00Z",
          verification_code: "482901",
        }),
      );
    return new Response(JSON.stringify(mailbox));
  });
  vi.stubGlobal("fetch", request);
  const localStorage = { getItem: vi.fn(), setItem: vi.fn() };
  vi.stubGlobal("localStorage", localStorage);

  render(<App />);
  fireEvent.click(screen.getAllByRole("button", { name: "创建临时邮箱" })[0]);

  expect(await screen.findByText("demo@example.com")).toBeVisible();
  expect(screen.getByText(/剩余约 1 小时/)).toBeVisible();
  expect(localStorage.getItem).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: "刷新收件箱" }));
  expect(await screen.findByRole("button", { name: /Your verification code/ })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: /Your verification code/ }));
  expect(await screen.findByText("来自 sender@example.com")).toBeVisible();
  expect(screen.getAllByText("Code: 482901").length).toBeGreaterThan(0);
  expect(screen.getByText("482901")).toBeVisible();
});

it("shows an expired mailbox and does not render upstream error details", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response('{"detail":"PUBLIC_MAILBOX_EXPIRED"}', { status: 410 })),
  );
  render(<App />);
  fireEvent.click(screen.getAllByRole("button", { name: "创建临时邮箱" })[0]);
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "这个临时邮箱已过期。",
  );
  expect(document.body).not.toHaveTextContent("PUBLIC_MAILBOX_EXPIRED");
});

it("copies a created address when clipboard is available", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(mailbox))),
  );
  const writeText = vi.fn(async () => undefined);
  Object.assign(navigator, { clipboard: { writeText } });
  render(<App />);
  fireEvent.click(screen.getAllByRole("button", { name: "创建临时邮箱" })[0]);
  await screen.findByText("demo@example.com");
  fireEvent.click(screen.getByRole("button", { name: "复制邮箱地址" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith("demo@example.com"));
  expect(screen.getByRole("status")).toHaveTextContent("邮箱地址已复制");
});
