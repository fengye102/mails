import { it, expect, vi } from "vitest";
import { apiFetch } from "./api";
it("rejects external paths before making a network request", async () => {
  const fetch = vi.fn();
  vi.stubGlobal("fetch", fetch);
  await expect(apiFetch("https://external.example")).rejects.toThrow();
  expect(fetch).not.toHaveBeenCalled();
});
it("uses same origin cookies and does not send CSRF on reads", async () => {
  const fetch = vi.fn(async () => new Response("[]"));
  vi.stubGlobal("fetch", fetch);
  await apiFetch("/mailboxes", {}, "csrf");
  const init = (fetch.mock.calls as unknown as [string, RequestInit][])[0][1];
  expect(init.credentials).toBe("same-origin");
  expect(new Headers(init.headers).has("X-CSRF-Token")).toBe(false);
});
it("does not surface upstream error bodies", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () => new Response('{"detail":"private-secret"}', { status: 502 }),
    ),
  );
  await expect(apiFetch("/connections")).rejects.toThrow(
    "操作失败，请检查配置后重试。",
  );
});
it("explains unavailable upstream status", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response('{"detail":"UPSTREAM_UNAVAILABLE"}', { status: 502 }),
    ),
  );
  await expect(apiFetch("/connections")).rejects.toThrow(
    "上游服务暂不可用，请稍后重试。",
  );
});
