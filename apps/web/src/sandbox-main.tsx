/** Dev-only component playground served at /sandbox.html. `vite dev` serves
 * extra root HTML pages automatically while production builds only include
 * `index.html`, so this entry never ships. */

import React from "react";
import ReactDOM from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "@wenyi/ui/app";
import { DocumentLanguage } from "@wenyi/ui/document-language";
import "@wenyi/ui/styles.css";
import { installMockPlatform, listScenarios, setScenario } from "../../../packages/ui/sandbox/mock-platform";
import { scenarioFromSearch } from "../../../packages/ui/sandbox/scenarios";

const params = new URLSearchParams(location.search);
const scenario = scenarioFromSearch(location.search);
const initialRoute = params.get("route") || "/";

installMockPlatform({ scenario: scenario.id });

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 5_000, refetchOnWindowFocus: false, retry: 1 },
  },
});

function ScenarioSwitcher({ active, onSelect }: { active: string; onSelect: (id: string) => void }) {
  return (
    <nav
      aria-label="Sandbox scenarios"
      className="fixed bottom-4 right-4 z-50 flex max-h-80 w-56 flex-col gap-1 overflow-y-auto rounded-lg border bg-background/95 p-3 shadow-e2 backdrop-blur"
    >
      <p className="text-xs font-semibold text-muted-foreground">Mock scenarios</p>
      {listScenarios().map((entry) => (
        <button
          key={entry.id}
          type="button"
          aria-pressed={entry.id === active}
          title={entry.description}
          onClick={() => onSelect(entry.id)}
          className={
            entry.id === active
              ? "rounded-md bg-primary px-2 py-1 text-left text-xs text-primary-foreground"
              : "rounded-md px-2 py-1 text-left text-xs hover:bg-muted"
          }
        >
          {entry.label}
        </button>
      ))}
    </nav>
  );
}

function SandboxRoot() {
  const [active, setActive] = React.useState(scenario.id);
  const select = (id: string) => {
    setScenario(id);
    setActive(id);
    const url = new URL(location.href);
    url.searchParams.set("scenario", id);
    history.replaceState(null, "", url);
    void queryClient.invalidateQueries();
  };
  return (
    <>
      <MemoryRouter initialEntries={[initialRoute]}>
        <DocumentLanguage />
        <App />
      </MemoryRouter>
      <ScenarioSwitcher active={active} onSelect={select} />
    </>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <SandboxRoot />
    </QueryClientProvider>
  </React.StrictMode>,
);
