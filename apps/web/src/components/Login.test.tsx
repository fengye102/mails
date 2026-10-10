import { render, screen, fireEvent } from "@testing-library/react";
import { it, expect, vi } from "vitest";
import { Login } from "./Login";
it("shows a safe login failure and clears password", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response('{"detail":"Invalid credentials"}', { status: 401 }),
    ),
  );
  render(<Login onLogin={vi.fn()} />);
  fireEvent.change(screen.getByLabelText("用户名"), {
    target: { value: "owner" },
  });
  fireEvent.change(screen.getByLabelText("密码"), {
    target: { value: "wrong" },
  });
  fireEvent.click(screen.getByRole("button", { name: "登录" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "用户名或密码错误",
  );
  expect(screen.getByLabelText("密码")).toHaveValue("");
});
