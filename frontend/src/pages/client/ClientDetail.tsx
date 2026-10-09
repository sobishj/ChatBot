// Client detail with tabs. Tabs a client_admin may not use are hidden here and enforced by the API.
import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, ApiError, type Client } from "../../api";
import { useAuth } from "../../auth";
import { PageHead } from "../../components/Layout";
import { Badge, Empty, Loading, Tabs } from "../../components/ui";
import { ApiActionsTab } from "./ApiActionsTab";
import { BrandingTab } from "./BrandingTab";
import { ConversationsTab } from "./ConversationsTab";
import { DocumentsTab } from "./DocumentsTab";
import { DomainsTab, EmbedTab, ModelTab } from "./SmallTabs";
import { OverviewTab } from "./OverviewTab";
import { StatsTab } from "./StatsTab";
import { TestChatTab } from "./TestChatTab";
import { WebsiteTab } from "./WebsiteTab";

export type TabProps = { client: Client; reload: () => Promise<void>; isSuper: boolean };

const ALL_TABS = [
  { key: "overview", label: "Overview" },
  { key: "website", label: "Website" },
  { key: "documents", label: "Documents" },
  { key: "model", label: "AI model" },
  { key: "branding", label: "Branding" },
  { key: "domains", label: "Allowed domains", superOnly: true },
  { key: "actions", label: "API actions" },
  { key: "test", label: "Test chat" },
  { key: "embed", label: "Embed code" },
  { key: "stats", label: "Stats" },
  { key: "conversations", label: "Conversations" },
] as const;
type TabKey = (typeof ALL_TABS)[number]["key"];

export function ClientDetail() {
  const { clientId = "", tab = "overview" } = useParams();
  const navigate = useNavigate();
  const { isSuper, mode } = useAuth();
  const [client, setClient] = useState<Client | null>(null);
  const [missing, setMissing] = useState(false);

  const reload = useCallback(async () => {
    try {
      const { client: c } = await api<{ client: Client }>(`/api/admin/clients/${clientId}`);
      setClient(c);
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) setMissing(true);
    }
  }, [clientId]);
  useEffect(() => {
    setClient(null);
    reload();
  }, [reload]);

  if (missing) return <div className="page"><Empty title="Client not found">It may have been deleted, or you don't have access. <Link to="/clients">Back to clients</Link></Empty></div>;
  if (!client) return <div className="page"><Loading /></div>;

  const tabs = ALL_TABS.filter((t) => !("superOnly" in t) || isSuper);
  const current = (tabs.some((t) => t.key === tab) ? tab : "overview") as TabKey;
  const props: TabProps = { client, reload, isSuper };

  return (
    <div className="page">
      <PageHead
        crumbs={mode === "onprem" ? undefined : <Link to="/clients">Clients</Link>}
        title={client.name}
        description={client.website_url ?? undefined}
        actions={client.active ? <Badge color="green"><span className="dot" />Active</Badge> : <Badge><span className="dot" />Inactive</Badge>}
      />
      <Tabs tabs={tabs.map((t) => ({ key: t.key, label: t.label }))} value={current} onChange={(k) => navigate(`/clients/${client.client_id}/${k}`)} />
      {current === "overview" && <OverviewTab {...props} />}
      {current === "website" && <WebsiteTab {...props} />}
      {current === "documents" && <DocumentsTab {...props} />}
      {current === "model" && <ModelTab {...props} />}
      {current === "branding" && <BrandingTab {...props} />}
      {current === "domains" && <DomainsTab {...props} />}
      {current === "actions" && <ApiActionsTab {...props} />}
      {current === "test" && <TestChatTab {...props} />}
      {current === "embed" && <EmbedTab {...props} />}
      {current === "stats" && <StatsTab {...props} />}
      {current === "conversations" && <ConversationsTab {...props} />}
    </div>
  );
}
