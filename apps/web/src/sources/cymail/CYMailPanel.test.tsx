import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { it, expect, vi } from "vitest";
import { CYMailPanel } from "./CYMailPanel";

type Route = (url: string, init?: RequestInit) => Response | undefined;
type Call = { url: string; init?: RequestInit };

function stub(routes: Route) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, init });
      const response = routes(url, init);
      if (!response) throw new Error(`unexpected request: ${url}`);
      return response;
    }),
  );
  return calls;
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status });

const connection = {
  id: "c1",
  kind: "cymail" as const,
  name: "CYMail 主号",
  base_url: "https://cy.example",
  status: "active",
  last_error: null,
  last_checked_at: null,
};

const item = {
  provider_key: "anon-1",
  address: "user@icloud.com",
  status: "active",
  created_at: "2026-07-09T02:32:10Z",
  detail: { label: "注册某网站" },
};

it("loads CYMail connection then lists hidden-email aliases", async () => {
  stub((url) => {
    if (url.endsWith("/connections")) return json([connection]);
    if (url.includes("/sources/cymail/aliases")) return json([item]);
    return undefined;
  });
  render(<CYMailPanel csrfToken="csrf" />);
  expect(await screen.findByText("user@icloud.com")).toBeVisible();
  expect(screen.getByText("可用")).toBeVisible();
  expect(screen.getByText("标签：注册某网站")).toBeVisible();
});

it("shows a hint when no CYMail connection exists", async () => {
  stub((url) => (url.endsWith("/connections") ? json([]) : undefined));
  render(<CYMailPanel csrfToken="csrf" />);
  expect(
    await screen.findByText("尚无 CYMail 连接，请先在「来源连接」登记。"),
  ).toBeVisible();
});

it("creates an alias with CSRF and never leaks the label into an error", async () => {
  const calls = stub((url, init) => {
    if (url.endsWith("/connections")) return json([connection]);
    if (init?.method === "POST" && url.includes("/sources/cymail/aliases"))
      return json(
        {
          provider_key: "fresh@icloud.com",
          address: "fresh@icloud.com",
          status: "active",
          created_at: "2026-07-09T02:32:10Z",
          detail: { label: "GitHub", interface: "icloud_web" },
        },
        201,
      );
    if (url.includes("/sources/cymail/aliases")) return json([]);
    return undefined;
  });
  render(<CYMailPanel csrfToken="csrf" />);
  await screen.findByText("该连接下暂无隐藏邮箱别名。");
  fireEvent.change(screen.getByLabelText("标签"), { target: { value: "GitHub" } });
  fireEvent.click(screen.getByRole("button", { name: "创建隐藏邮箱" }));

  expect(await screen.findByText("fresh@icloud.com")).toBeVisible();
  expect(screen.getByLabelText("标签")).toHaveValue("");
  const post = calls.find((call) => call.init?.method === "POST")!;
  expect(post.url).toContain("/sources/cymail/aliases");
  expect(post.url).toContain("connection_id=c1");
  expect(JSON.parse(post.init!.body as string)).toEqual({ label: "GitHub" });
  expect(new Headers(post.init!.headers).get("X-CSRF-Token")).toBe("csrf");
});

it("deactivates an alias and reflects the new status", async () => {
  const calls = stub((url, init) => {
    if (url.endsWith("/connections")) return json([connection]);
    if (init?.method === "POST" && url.includes("/deactivate"))
      return json({ provider_key: "anon-1", success: true });
    if (url.includes("/sources/cymail/aliases")) return json([item]);
    return undefined;
  });
  render(<CYMailPanel csrfToken="csrf" />);
  await screen.findByText("user@icloud.com");
  fireEvent.click(screen.getByRole("button", { name: "停用" }));

  expect(await screen.findByText("已停用")).toBeVisible();
  const post = calls.find(
    (call) => call.init?.method === "POST" && call.url.includes("/deactivate"),
  )!;
  expect(post.url).toContain("/sources/cymail/aliases/anon-1/deactivate");
  expect(new Headers(post.init!.headers).get("X-CSRF-Token")).toBe("csrf");
});

it("deletes an alias and removes it from the list", async () => {
  const calls = stub((url, init) => {
    if (url.endsWith("/connections")) return json([connection]);
    if (init?.method === "DELETE") return new Response(null, { status: 204 });
    if (url.includes("/sources/cymail/aliases")) return json([item]);
    return undefined;
  });
  render(<CYMailPanel csrfToken="csrf" />);
  await screen.findByText("user@icloud.com");
  fireEvent.click(screen.getByRole("button", { name: "删除" }));

  await waitFor(() =>
    expect(screen.queryByText("user@icloud.com")).not.toBeInTheDocument(),
  );
  const removed = calls.find((call) => call.init?.method === "DELETE")!;
  expect(removed.url).toContain("/sources/cymail/aliases/anon-1");
  expect(removed.url).toContain("connection_id=c1");
  expect(new Headers(removed.init!.headers).get("X-CSRF-Token")).toBe("csrf");
});
