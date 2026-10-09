import { render, screen, fireEvent } from "@testing-library/react";
import { it, expect, vi } from "vitest";
import { Mailboxes } from "./Mailboxes";
const mailbox = {
  id: "b1",
  address: "one@example.com",
  sync_state: "idle",
  last_synced_at: null,
};
it("explains duplicate mailbox registration", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async (path: string, init?: RequestInit) =>
        new Response(
          JSON.stringify(
            init?.method === "POST"
              ? { detail: "MAILBOX_ALREADY_REGISTERED" }
              : path.endsWith("/connections")
                ? [{ id: "c1", name: "办公" }]
                : [],
          ),
          { status: init?.method === "POST" ? 409 : 200 },
        ),
    ),
  );
  render(<Mailboxes csrfToken="csrf" />);
  await screen.findByRole("option", { name: "办公" });
  fireEvent.change(screen.getByLabelText("来源连接"), {
    target: { value: "c1" },
  });
  fireEvent.change(screen.getByLabelText("邮箱地址"), {
    target: { value: "one@example.com" },
  });
  fireEvent.click(screen.getByRole("button", { name: "登记邮箱" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("该邮箱已登记");
});
it.each(["partial", "failed"])(
  "shows %s sync results without losing mailbox",
  async (status) => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (path: string, init?: RequestInit) =>
          new Response(
            JSON.stringify(
              init?.method === "POST"
                ? {
                    id: "r1",
                    mailbox_id: "b1",
                    status,
                    error_code: "UPSTREAM_TIMEOUT",
                    new_message_count: 1,
                  }
                : path.endsWith("/mailboxes")
                  ? [mailbox]
                  : [],
            ),
          ),
      ),
    );
    render(<Mailboxes csrfToken="csrf" />);
    fireEvent.click(
      await screen.findByRole("button", { name: "同步 one@example.com" }),
    );
    expect(
      await screen.findByText(status === "partial" ? "部分成功" : "同步失败"),
    ).toBeVisible();
    expect(screen.getByText("上游响应超时，请重试。")).toBeVisible();
    expect(screen.getByText("one@example.com")).toBeVisible();
  },
);
