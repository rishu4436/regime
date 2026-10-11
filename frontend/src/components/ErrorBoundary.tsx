import { Component, type ErrorInfo, type ReactNode } from "react";

/** Keeps one broken panel from blanking the whole dashboard: shows the error and a retry instead. */
export class ErrorBoundary extends Component<{ children: ReactNode; label?: string }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };
  static getDerivedStateFromError(error: Error) { return { error }; }
  componentDidCatch(error: Error, info: ErrorInfo) { console.error(`[${this.props.label ?? "view"}] render error`, error, info.componentStack); }
  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="banner red" role="alert" style={{ marginBottom: 12 }}>
        <span className="grow small"><b>Part of the {this.props.label ?? "page"} couldn't be shown.</b> The agent keeps running; this only affects the display.
          <br /><span className="faint">{error.message}</span></span>
        <button className="btn ghost" style={{ padding: "2px 10px" }} onClick={() => this.setState({ error: null })}>Retry</button>
      </div>
    );
  }
}
