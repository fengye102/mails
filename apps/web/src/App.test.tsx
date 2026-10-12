import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { it, expect, vi } from "vitest";
import { App } from "./App";
it("restores session and logs out on a subsequent 401", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async (path: string) =>
        new Response(
          JSON.stringify(
            path.endsWith("/session")
              ? { user: { username: "owner" }, csrf_token: "csrf" }
              : { detail: "expired" },
          ),
          { status: path.endsWith("/session") ? 200 : 401 },
        ),
    ),
  );
  render(<App />);
  expect(await screen.findByRole("button", { name: "登录" })).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "退出登录" }),
  ).not.toBeInTheDocument();
});
it("revokes server session on logout", async () => {
  const request = vi.fn(async (path: string, init?: RequestInit) =>
    init?.method === "DELETE"
      ? new Response(null, { status: 204 })
      : new Response(
          JSON.stringify(
            path.endsWith("/session")
              ? { user: { username: "owner" }, csrf_token: "csrf" }
              : path.includes("/messages")
                ? { items: [], total: 0 }
                : [],
          ),
        ),
  );
  vi.stubGlobal("fetch", request);
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "退出登录" }));
  await screen.findByRole("button", { name: "登录" });
  await waitFor(() =>
    expect(request.mock.calls.some((c) => c[1]?.method === "DELETE")).toBe(
      true,
    ),
  );
});
