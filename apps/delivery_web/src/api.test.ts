import { describe, expect, it, vi } from "vitest";
import {
  errorText,
  formatRemaining,
  readMailbox,
  verifyPickup,
  verifyPickupKey,
} from "./api";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("verifyPickup", () => {
  it("用取件码调用验证接口并返回取件会话", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        pickup_id: "pickup-1",
        allocation_id: "allocation-1",
        session_token: "session-1",
        expires_at: "2026-10-11T12:00:00+00:00",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const session = await verifyPickup("7F3K-92QD");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/delivery-api/v1/pickups/verify");
    expect(init.method).toBe("POST");
    expect(init.body).toBe(JSON.stringify({ pickup_code: "7F3K-92QD" }));
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
    expect(session.allocation_id).toBe("allocation-1");
  });

  it("取件码无效时给出固定中文提示", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: { code: "DELIVERY_PICKUP_INVALID" } }, 401)),
    );

    await expect(verifyPickup("wrong")).rejects.toThrow(errorText("DELIVERY_PICKUP_INVALID"));
  });

  it("退避期间给出稍后再试提示", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(jsonResponse({ detail: { code: "DELIVERY_PICKUP_RETRY_LATER" } }, 429)),
    );

    await expect(verifyPickup("wrong")).rejects.toThrow("尝试次数过多，请稍后再试。");
  });

  it("校验错误（detail 为数组）降级为通用文案", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: [{ msg: "too short" }] }, 422)),
    );

    await expect(verifyPickup("x")).rejects.toThrow("操作失败，请稍后重试。");
  });

  it("网络异常给出可重试提示", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("failed")));

    await expect(verifyPickup("7F3K")).rejects.toThrow("网络连接失败，请重试。");
  });
});

describe("readMailbox", () => {
  it("带 Bearer 会话令牌读取分配邮箱", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ address: "box@example.test", messages: [] }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const mailbox = await readMailbox("allocation/1", "session-1");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/delivery-api/v1/pickups/allocation%2F1/mailbox");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer session-1");
    expect(mailbox.address).toBe("box@example.test");
  });

  it("会话失效时给出重新输入的提示", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          jsonResponse({ detail: { code: "DELIVERY_PICKUP_SESSION_INVALID" } }, 401),
        ),
    );

    await expect(readMailbox("a", "t")).rejects.toThrow("取件会话已失效，请重新输入取件码。");
  });
});

describe("formatRemaining", () => {
  it("按剩余时间给出可读文案", () => {
    expect(formatRemaining(0)).toBe("已过期");
    expect(formatRemaining(45)).toBe("剩余 45 秒");
    expect(formatRemaining(600)).toBe("剩余约 10 分钟");
    expect(formatRemaining(3900)).toBe("剩余约 1 小时 5 分钟");
  });
});

describe("verifyPickupKey", () => {
  it("用取件密钥与地址调用验证接口", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        pickup_id: "pickup-1",
        allocation_id: "allocation-1",
        session_token: "session-1",
        expires_at: "2026-10-11T12:00:00+00:00",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const session = await verifyPickupKey("tok_abc", "box@example.test");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/delivery-api/v1/pickups/verify");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({
      pickup_key: "tok_abc",
      email: "box@example.test",
    });
    expect(session.allocation_id).toBe("allocation-1");
  });

  it("不带地址时只提交密钥", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        pickup_id: "pickup-1",
        allocation_id: "allocation-1",
        session_token: "session-1",
        expires_at: "2026-10-11T12:00:00+00:00",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await verifyPickupKey("tok_abc");

    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ pickup_key: "tok_abc" });
  });

  it("凭证缺失时给出固定中文提示", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({ detail: { code: "DELIVERY_PICKUP_CREDENTIAL_REQUIRED" } }, 400),
      ),
    );

    await expect(verifyPickup("")).rejects.toThrow(
      "请输入取件码，或直接打开收到的取件链接。",
    );
  });
});
