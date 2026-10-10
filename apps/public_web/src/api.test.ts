import { expect, it, vi } from "vitest";
import { apiFetch } from "./api";

it("uses the isolated public API and cookie credentials", async () => {
  const request = vi.fn(async () => new Response("{}"));
  vi.stubGlobal("fetch", request);

  await apiFetch("/mailboxes");

  expect(request).toHaveBeenCalledWith(
    "/public-api/v1/mailboxes",
    expect.objectContaining({ credentials: "include", cache: "no-store" }),
  );
});

it("rejects external request paths before making a request", async () => {
  const request = vi.fn();
  vi.stubGlobal("fetch", request);

  await expect(apiFetch("https://outside.example")).rejects.toThrow();
  expect(request).not.toHaveBeenCalled();
});

it("maps fixed public API errors without exposing upstream details", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      new Response('{"detail":"UPSTREAM_SECRET_BODY"}', { status: 410 }),
    ),
  );

  await expect(apiFetch("/mailboxes/box-1")).rejects.toThrow(
    "这个临时邮箱已过期。",
  );
  expect(document.body).not.toHaveTextContent("UPSTREAM_SECRET_BODY");
});
