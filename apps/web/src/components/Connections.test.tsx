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
