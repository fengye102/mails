import { render, screen } from "@testing-library/react";
import { it, expect, vi } from "vitest";
import { CloudflareTempEmailPage } from "./CloudflareTempEmailPage";

it("only offers Cloudflare connections and renders the panel for the selection", async () => {
  const request = vi.fn(async (path: string) => {
    if (path === "/api/v1/connections")
      return new Response(
        JSON.stringify([
          {
            id: "cf-1",
            kind: "cloudflare_temp_email",
            name: "临时邮箱",
            base_url: "https://temp.example",
            status: "active",
            last_error: null,
            last_checked_at: null,
          },
          {
            id: "cy-1",
            kind: "cymail",
            name: "CYMail",
            base_url: "https://cy.example",
            status: "active",
            last_error: null,
            last_checked_at: null,
          },
        ]),
        { status: 200 },
      );
    if (path === "/api/v1/sources/cloudflare_temp_email/cf-1/items")
      return new Response(
        JSON.stringify([
          {
            provider_key: "125",
            address: "tmp809a7130cd07@orangeai.cfd",
            status: "active",
            created_at: "2026-10-11T12:47:08Z",
            detail: {},
          },
        ]),
        { status: 200 },
      );
    return new Response("[]", { status: 200 });
  });
  vi.stubGlobal("fetch", request);
  render(<CloudflareTempEmailPage csrfToken="csrf" />);

  expect(await screen.findByRole("option", { name: /临时邮箱/ })).toBeVisible();
  expect(screen.queryByRole("option", { name: /CYMail/ })).toBeNull();
  expect(await screen.findByText("tmp809a7130cd07@orangeai.cfd")).toBeVisible();
});

it("shows a hint when no Cloudflare connection exists", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response("[]", { status: 200 })),
  );
  render(<CloudflareTempEmailPage csrfToken="csrf" />);
  expect(
    await screen.findByText("尚无 Cloudflare 临时邮箱来源连接，请先在「来源连接」里登记。"),
  ).toBeVisible();
});
