// src/App.tsx
import React from "react";
import { ImageSearchWidget } from "./ImageSearchWidget";

export default function App() {
  return (
    <div
      style={{
        fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, sans-serif",
        padding: 16,
        maxWidth: 600,
        margin: "0 auto",
      }}
    >
      <h2 style={{ marginBottom: 8 }}>Weaviate Image Search</h2>
      <p style={{ marginTop: 0, marginBottom: 16, fontSize: 14 }}>
        Carica un&apos;immagine, la inviamo al tuo server MCP e usiamo{" "}
        <code>image_search_vertex</code> sulla collection <strong>Sinde</strong>.
      </p>

      <ImageSearchWidget />
    </div>
  );
}
