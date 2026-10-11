import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { App } from "./App";

const session = {
  pickup_id: "pickup-1",
  allocation_id: "allocation-1",
  session_token: "session-1",
  expires_at: new Date(Date.now() + 600_000).toISOString(),
};

const mailbox = {
  address: "delivered@example.test",
  messages: [
    {
      id: "m1",
      sender: "noreply@example.test",
      subject: "登录验证码",
      received_at: "2026-10-11T04:00:00+00:00",
      body_text: "您的验证码是 246810，五分钟内有效。",
      verification_code: "246810",
    },
  ],
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubFetch() {
  const request = vi.fn(async (path: string) =>
    path.includes("/pickups/verify") ? jsonResponse(session) : jsonResponse(mailbox),
  );
  vi.stubGlobal("fetch", request);
  return request;
}

async function submitCode(code: string) {
  fireEvent.change(screen.getByLabelText("取件码"), { target: { value: code } });
  fireEvent.click(screen.getByRole("button", { name: "取件" }));
}

describe("取件页", () => {
  it("验证取件码后展示邮箱地址、邮件正文与验证码", async () => {
    const request = stubFetch();
    render(<App />);

    await submitCode("7F3K-92QD");

    expect(await screen.findByText("delivered@example.test")).toBeInTheDocument();
    expect(screen.getByText("登录验证码")).toBeInTheDocument();
    expect(screen.getByText("246810")).toBeInTheDocument();
    expect(screen.getByText(/您的验证码是 246810/)).toBeInTheDocument();
    expect(request).toHaveBeenCalledTimes(2);
    expect(String(request.mock.calls[1][0])).toBe(
      "/delivery-api/v1/pickups/allocation-1/mailbox",
    );
  });

  it("取件码无效时不进入取件状态", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({ detail: { code: "DELIVERY_PICKUP_INVALID" } }, 401),
      ),
    );
    render(<App />);

    await submitCode("WRONG");

    expect(await screen.findByRole("alert")).toHaveTextContent("取件码无效、已使用或已过期。");
    expect(screen.getByLabelText("取件码")).toBeInTheDocument();
  });

  it("空取件码不发请求", async () => {
    const request = vi.fn();
    vi.stubGlobal("fetch", request);
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "取件" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("请输入取件码。");
    expect(request).not.toHaveBeenCalled();
  });

  it("刷新邮件会重新读取邮箱", async () => {
    const request = stubFetch();
    render(<App />);

    await submitCode("7F3K-92QD");
    await screen.findByText("delivered@example.test");

    fireEvent.click(screen.getByRole("button", { name: "刷新邮件" }));

    await waitFor(() => expect(request).toHaveBeenCalledTimes(3));
  });

  it("退出取件会清空会话并回到取件码输入", async () => {
    stubFetch();
    render(<App />);

    await submitCode("7F3K-92QD");
    await screen.findByText("delivered@example.test");

    fireEvent.click(screen.getByRole("button", { name: "退出取件" }));

    expect(screen.getByLabelText("取件码")).toBeInTheDocument();
    expect(screen.queryByText("delivered@example.test")).not.toBeInTheDocument();
  });
});
