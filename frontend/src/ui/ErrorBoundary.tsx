// Error boundaries: a failure inside one view shows a message in that view's cell; a failure of
// the whole app shows a page with a way out (reset the saved workspace), never a blank screen.

import { Component, type ErrorInfo, type ReactNode } from 'react';

import { STORAGE_KEY } from '../state/workspace';
import { code, event } from '../telemetry/telemetry';

interface Props {
  scope: 'app' | 'view';
  children: ReactNode;
  /** Rendered instead of the children after an error (views use it to offer "Close"). */
  fallback?: (error: Error) => ReactNode;
}

export class ErrorBoundary extends Component<Props, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Only the error type and the first component frame (code locations, never data).
    const frame = info.componentStack?.trim().split('\n')[0]?.trim() ?? '';
    event('ui_error', null, { kind: this.props.scope, type: code(error.name), code: code(frame.slice(0, 80)) });
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    if (this.props.fallback) return this.props.fallback(error);
    return (
      <div className="crash" role="alert">
        <h1>Something went wrong</h1>
        <p>The app hit an error it could not recover from. Reloading usually helps; if the same error comes back, start with an empty workspace.</p>
        <p className="muted small">{error.name}: {error.message}</p>
        <div className="crash-actions">
          <button className="primary" onClick={() => window.location.reload()}>
            Reload
          </button>
          <button
            className="ghost"
            onClick={() => {
              try {
                localStorage.removeItem(STORAGE_KEY);
              } catch {
                // storage unavailable: nothing saved to remove
              }
              window.location.reload();
            }}
          >
            Reset workspace and reload
          </button>
        </div>
      </div>
    );
  }
}
