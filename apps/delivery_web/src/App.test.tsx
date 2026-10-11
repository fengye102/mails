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
  sync: { messages_total: 1, last_ingested_at: "2026-10-11T04:00:05+00:00" },
  messages: [
    {
      id: "m1",
      sender: "noreply@example.test",
      subject: "登录验证码",
      received_at: "2026-10-11T04:00:00+00:00",
      body_text: "您的验证码是 246810，五分钟内有效。",
      verification_code: "246810",
      link: null,
    },
  ],
};

/** 链接型激活邮件：没有数字验证码，唯一有用的东西是行动链接。 */
const linkMailbox = {
  address: "activation@example.test",
  sync: { messages_total: 1, last_ingested_at: null },
  messages: [
    {
      id: "m2",
      sender: "noreply@openxlab.org.cn",
      subject: "激活你的账号",
      received_at: "2026-10-11T05:00:00+00:00",
      body_text: "点击激活 (https://sso.openxlab.org.cn/active?token=zz)",
      verification_code: null,
      link: "https://sso.openxlab.org.cn/active?token=zz",
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

  it("打开取件链接即取件，并立刻清掉地址栏里的密钥", async () => {
    const request = stubFetch();
    window.history.replaceState(
      {},
      "",
      "/pickup#email=delivered%40example.test&key=tok_abc123",
    );

    render(<App />);

    expect(await screen.findByText("delivered@example.test")).toBeInTheDocument();
    const [url, init] = request.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/delivery-api/v1/pickups/verify");
    expect(JSON.parse(String(init.body))).toEqual({
      pickup_key: "tok_abc123",
      email: "delivered@example.test",
    });
    // 密钥不能留在地址栏（历史记录、截图、转发都会泄漏）
    expect(window.location.hash).toBe("");
  });

  it("链接型激活邮件展示可点击的行动链接并可复制", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (path: string) =>
        path.includes("/pickups/verify") ? jsonResponse(session) : jsonResponse(linkMailbox),
      ),
    );
    render(<App />);

    await submitCode("7F3K-92QD");

    const link = await screen.findByRole("link", {
      name: "https://sso.openxlab.org.cn/active?token=zz",
    });
    expect(link).toHaveAttribute("href", "https://sso.openxlab.org.cn/active?token=zz");
    expect(link).toHaveAttribute("rel", expect.stringContaining("noreferrer"));

    fireEvent.click(screen.getByRole("button", { name: "复制链接" }));
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith("https://sso.openxlab.org.cn/active?token=zz"),
    );
  });

  it("如实展示入库状态，没有入库记录时不假装已同步", async () => {
    stubFetch();
    render(<App />);

    await submitCode("7F3K-92QD");

    expect(await screen.findByText(/已入库 1 封 · 最近更新/)).toBeInTheDocument();

    vi.stubGlobal(
      "fetch",
      vi.fn(async (path: string) =>
        path.includes("/pickups/verify") ? jsonResponse(session) : jsonResponse(linkMailbox),
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "退出取件" }));
    await submitCode("7F3K-92QD");

    expect(await screen.findByText(/已入库 1 封 · 暂无入库记录/)).toBeInTheDocument();
  });
});
