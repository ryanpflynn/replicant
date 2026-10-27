import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import "./styles/tokens.css";
import "./styles/app.css";
import "./styles/web.css";
import { App } from "./App";
import { Router } from "./router";
import { makeQueryClient, SessionProvider } from "./lib/session";

const qc = makeQueryClient();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={qc}>
      <Router>
        <SessionProvider>
          <App />
        </SessionProvider>
      </Router>
    </QueryClientProvider>
  </StrictMode>,
);
