import { Component } from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import "./styles.css";

// A mistake in one view must not leave an empty window: say what broke, and
// offer the way back.
class Guard extends Component {
  state = { error: null };

  static getDerivedStateFromError(error) {
    return { error };
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="crashed">
        <p>Something in the window broke:</p>
        <pre>{String(this.state.error)}</pre>
        <button onClick={() => location.reload()}>reload</button>
      </div>
    );
  }
}

createRoot(document.getElementById("root")).render(<Guard><App /></Guard>);
