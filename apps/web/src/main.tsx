import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "@wenyi/ui/App";
import { DocumentLanguage } from "@wenyi/ui/i18n/DocumentLanguage";
import { configurePlatform } from "@wenyi/ui/platform";
import "@wenyi/ui/index.css";
import { webPlatform } from "./platform";

configurePlatform(webPlatform);

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 5_000, refetchOnWindowFocus: false, retry: 1 },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <DocumentLanguage />
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
