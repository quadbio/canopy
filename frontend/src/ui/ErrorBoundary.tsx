// Error boundaries: a failure inside one view shows a message in that view's cell; a failure of
// the whole app shows a page with a way out (reset the saved workspace), never a blank screen.

import { Component, type ErrorInfo, type ReactNode } from 'react';

import { STORAGE_KEY } from '../state/workspace';
import { code, event, messageCode, where } from '../telemetry/telemetry';

interface Props {
  scope: 'app' | 'view';
  children: ReactNode;
  /** Rendered instead of the children after an error (views use it to offer "Close"). */
  fallback?: (error: Error) => ReactNode;
  /** When it changes (e.g. the view's settings), an error is cleared and the children try again
   * (review full-2 C-M4: picking another gene left "This view failed"). */
  resetKey?: unknown;
}

export class ErrorBoundary extends Component<Props, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidUpdate(prev: Props) {
    if (this.state.error && prev.resetKey !== this.props.resetKey) this.setState({ error: null });
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // The error type, its message without names and where it happened (code locations only).
    event('ui_error', null, {
      kind: this.props.scope,
      type: code(error.name),
      message: messageCode(error.message),
      where: where(error.stack ?? info.componentStack ?? undefined),
    });
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
