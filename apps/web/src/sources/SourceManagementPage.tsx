import { useEffect, useState } from "react";
import { apiFetch, type Connection } from "../api";
import { CYMailPanel } from "./cymail/CYMailPanel";
import { CloudflareTempEmailPage } from "./cloudflare_temp_email/CloudflareTempEmailPage";
import { OutlookPlusPanel } from "./outlook_plus/OutlookPlusPanel";

const tabs = ["iCloud HME", "临时邮箱", "Outlook 邮箱池"] as const;
export type SourceTab = (typeof tabs)[number];

/** 来源管理入口：把三个来源的管理面板收在一个页签下，避免后台被切成好几块。 */
export function SourceManagementPage({ csrfToken }: { csrfToken: string }) {
  const [tab, setTab] = useState<SourceTab>("iCloud HME");
  const [connections, setConnections] = useState<Connection[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    void apiFetch<Connection[]>("/connections")
      .then((items) => {
        if (active) setConnections(items);
      })
      .catch((caught) => {
        if (active) setError((caught as Error).message);
      });
    return () => {
      active = false;
    };
  }, []);
  const outlookPlusId =
    connections.find((item) => item.kind === "outlook_plus")?.id ?? "";
  return (
    <section className="source-management">
      <div className="source-tabs" role="tablist" aria-label="来源切换">
        {tabs.map((name) => (
          <button
            key={name}
            role="tab"
            aria-selected={tab === name}
            aria-current={tab === name ? "page" : undefined}
            onClick={() => setTab(name)}
          >
            {name}
          </button>
        ))}
      </div>
      {error && <p role="alert">{error}</p>}
      {tab === "iCloud HME" && <CYMailPanel csrfToken={csrfToken} />}
      {tab === "临时邮箱" && <CloudflareTempEmailPage csrfToken={csrfToken} />}
      {tab === "Outlook 邮箱池" &&
        (outlookPlusId ? (
          <OutlookPlusPanel connectionId={outlookPlusId} csrfToken={csrfToken} />
        ) : (
          <p role="status">
            还没有 Outlook 邮箱池连接，请先在「来源连接」里添加后再回到这里管理账号。
          </p>
        ))}
    </section>
  );
}
