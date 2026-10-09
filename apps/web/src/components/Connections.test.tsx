import { render, screen, fireEvent } from "@testing-library/react";
import { it, expect, vi } from "vitest";
import { Connections } from "./Connections";
it("submits key only in request and clears input even on error", async () => {
  const request = vi.fn(
    async (_path: string, init?: RequestInit) =>
      new Response(init?.method === "POST" ? '{"detail":"secret-key"}' : "[]", {
        status: init?.method === "POST" ? 502 : 200,
      }),
  );
  vi.stubGlobal("fetch", request);
  render(<Connections csrfToken="csrf" />);
  await screen.findByText("尚无连接，请先登记 OutlookEmailPlus 服务。");
  fireEvent.change(screen.getByLabelText("连接名称"), {
    target: { value: "办公" },
  });
  fireEvent.change(screen.getByLabelText("服务地址"), {
    target: { value: "https://mail.example" },
  });
  fireEvent.change(screen.getByLabelText("API Key"), {
    target: { value: "secret-key" },
  });
  fireEvent.click(screen.getByRole("button", { name: "验证并添加连接" }));
  expect(await screen.findByRole("alert")).not.toHaveTextContent("secret-key");
  expect(screen.getByLabelText("API Key")).toHaveValue("");
  expect(document.body).not.toHaveTextContent("secret-key");
  const init = request.mock.calls.find((c) => c[1]?.method === "POST")![1]!;
  expect(JSON.parse(init.body as string).api_key).toBe("secret-key");
  expect(new Headers(init.headers).get("X-CSRF-Token")).toBe("csrf");
  expect(init.credentials).toBe("same-origin");
});

it("submits the selected Cloudflare source without exposing its secret", async () => {
  const request = vi.fn(async (_path: string, init?: RequestInit) =>
    new Response(init?.method === "POST" ? JSON.stringify({
      id: "c1",
      kind: "cloudflare_temp_email",
      name: "临时邮箱",
      base_url: "https://temp.example",
      status: "active",
      last_error: null,
      last_checked_at: null,
    }) : "[]", { status: 200 }),
  );
  vi.stubGlobal("fetch", request);
  render(<Connections csrfToken="csrf" />);
  await screen.findByText("尚无连接，请先登记 OutlookEmailPlus 服务。");

  fireEvent.change(screen.getByLabelText("来源类型"), {
    target: { value: "cloudflare_temp_email" },
  });
  fireEvent.change(screen.getByLabelText("连接名称"), {
    target: { value: "临时邮箱" },
  });
  fireEvent.change(screen.getByLabelText("服务地址"), {
    target: { value: "https://temp.example" },
  });
  fireEvent.change(screen.getByLabelText("API Key"), {
    target: { value: "cloudflare-secret" },
  });
  fireEvent.click(screen.getByRole("button", { name: "验证并添加连接" }));

  expect(await screen.findByText("临时邮箱")).toBeVisible();
  expect(document.body).not.toHaveTextContent("cloudflare-secret");
  const init = request.mock.calls.find((call) => call[1]?.method === "POST")![1]!;
  expect(JSON.parse(init.body as string)).toMatchObject({
    kind: "cloudflare_temp_email",
    api_key: "cloudflare-secret",
    config: {},
  });
});

it("sends CYMail account id as non-secret connection configuration", async () => {
  const request = vi.fn(async (_path: string, init?: RequestInit) =>
    new Response(init?.method === "POST" ? JSON.stringify({
      id: "c2",
      kind: "cymail",
      name: "CYMail",
      base_url: "https://cymail.example",
      status: "active",
      last_error: null,
      last_checked_at: null,
      config_summary: { account_id: "account-42" },
    }) : "[]", { status: 200 }),
  );
  vi.stubGlobal("fetch", request);
  render(<Connections csrfToken="csrf" />);
  await screen.findByText("尚无连接，请先登记 OutlookEmailPlus 服务。");

  fireEvent.change(screen.getByLabelText("来源类型"), {
    target: { value: "cymail" },
  });
  fireEvent.change(screen.getByLabelText("连接名称"), {
    target: { value: "CYMail" },
  });
  fireEvent.change(screen.getByLabelText("服务地址"), {
    target: { value: "https://cymail.example" },
  });
  fireEvent.change(screen.getByLabelText("API Key"), {
    target: { value: "cymail-secret" },
  });
  fireEvent.change(screen.getByLabelText("账户 ID"), {
    target: { value: "account-42" },
  });
  fireEvent.click(screen.getByRole("button", { name: "验证并添加连接" }));

  expect(await screen.findByText("CYMail")).toBeVisible();
  const init = request.mock.calls.find((call) => call[1]?.method === "POST")![1]!;
  expect(JSON.parse(init.body as string)).toMatchObject({
    kind: "cymail",
    api_key: "cymail-secret",
    config: { account_id: "account-42" },
  });
  expect(document.body).not.toHaveTextContent("cymail-secret");
});
