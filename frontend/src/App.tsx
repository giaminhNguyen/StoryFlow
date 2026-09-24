import { useState } from "react";
import { defaultClient, type ApiClient } from "./api/client";
import { ConnectionBanner } from "./components/ConnectionBanner";
import { HealthChip } from "./components/HealthChip";
import { ProviderChip } from "./components/ProvidersPanel";
import { AppProvider } from "./connection";
import { usePolling } from "./hooks/usePolling";
import { ProjectDetailPage } from "./pages/ProjectDetailPage";
import { WorkflowDetailPage } from "./pages/WorkflowDetailPage";
import { WorkflowListPage } from "./pages/WorkflowListPage";
import { useHashRoute } from "./router";

export function App({ client = defaultClient }: { client?: ApiClient }) {
  const route = useHashRoute();
  const health = usePolling((signal) => client.health(signal), { intervalMs: 5000 });
  const providers = usePolling((signal) => client.providers(signal), { intervalMs: 10000 });
  const [pageDown, setPageDown] = useState(false);
  const disconnected = pageDown || !health.connected;

  let page;
  switch (route.name) {
    case "list":
      page = <WorkflowListPage client={client} />;
      break;
    case "workflow":
      page = <WorkflowDetailPage key={route.id} workflowId={route.id} client={client} />;
      break;
    case "project":
      page = <ProjectDetailPage key={route.id} projectId={route.id} client={client} />;
      break;
    default:
      page = (
        <section>
          <h1>Page not found</h1>
          <p>There is nothing at this address. <a href="#/">Back to workflows</a></p>
        </section>
      );
  }

  return (
    <AppProvider health={health.data} providers={providers.data} onDisconnectedChange={setPageDown}>
      <header className="app-header">
        <a href="#/" className="app-name">StoryFlow</a>
        <HealthChip health={health.data} connected={health.connected} />
        <ProviderChip />
      </header>
      <ConnectionBanner disconnected={disconnected} />
      <main>{page}</main>
    </AppProvider>
  );
}
