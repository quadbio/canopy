import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { App } from './app/App';
import { restoreWorkspace } from './state/workspace';
import './styles/tokens.css';
import './styles/app.css';
import { event, installErrorReporting } from './telemetry/telemetry';
import { ErrorBoundary } from './ui/ErrorBoundary';
import './views';

installErrorReporting();
restoreWorkspace(); // after './views' has registered every view type
event('page_load', performance.now());

const queryClient = new QueryClient({
  defaultOptions: { queries: { refetchOnWindowFocus: false } },
});

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary scope="app">
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>
    </ErrorBoundary>
  </StrictMode>,
);
