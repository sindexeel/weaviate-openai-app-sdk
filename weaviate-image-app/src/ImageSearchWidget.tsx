// src/ImageSearchWidget.tsx
import React, { useState } from "react";

// URL base del tuo server MCP (quello con serve.py)
const MCP_BASE_URL = "https://weaviate-openai-app-sdk.onrender.com";

type SearchResult = {
  uuid?: string;
  properties?: {
    name?: string;
    source_pdf?: string;
    page_index?: number;
    mediaType?: string;
    [key: string]: any;
  };
  distance?: number;
};

// Declare global types per window.openai
declare global {
  interface Window {
    openai?: {
      createClient?: () => {
        tools: {
          call(args: { name: string; arguments?: any }): Promise<any>;
        };
      } | null | undefined;
    };
  }
}

// 1) client SDK UNA VOLTA sola (inizializzato all'avvio)
let client: any = null;
try {
  if (window.openai?.createClient) {
    client = window.openai.createClient();
  }
} catch (e) {
  console.warn("⚠️ window.openai.createClient non disponibile:", e);
}

export const ImageSearchWidget: React.FC = () => {
  const [file, setFile] = useState<File | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [results, setResults] = useState<SearchResult[] | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0] ?? null;
    setFile(f);
    setResults(null);
    setStatus(null);
  };

  const handleUploadAndSearch = async () => {
    if (!file) {
      setStatus("Seleziona prima un'immagine.");
      return;
    }

    try {
      setIsLoading(true);
      setStatus("Caricamento dell'immagine in corso...");

      // 1️⃣ Upload immagine al tuo endpoint /upload-image (HTTP, non MCP tool)
      const form = new FormData();
      form.append("image", file); // il campo deve chiamarsi "image"

      const uploadResp = await fetch(`${MCP_BASE_URL}/upload-image`, {
        method: "POST",
        body: form,
      });

      if (!uploadResp.ok) {
        const text = await uploadResp.text();
        throw new Error(
          `Upload fallito (${uploadResp.status}): ${text || "errore sconosciuto"}`
        );
      }

      const uploadData = await uploadResp.json();
      const imageId = uploadData.image_id as string | undefined;

      if (!imageId) {
        throw new Error("Risposta /upload-image senza image_id");
      }

      setStatus(`Immagine caricata (image_id = ${imageId}). Avvio la ricerca...`);

      // 2️⃣ Chiama il backend HTTP /image-search (non più MCP)
      const searchResp = await fetch(`${MCP_BASE_URL}/image-search`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          collection: "Sinde",
          image_id: imageId,
          limit: 10,
        }),
      });

      if (!searchResp.ok) {
        const err = await searchResp.json().catch(() => ({}));
        throw new Error(err.error || "Errore nella ricerca immagini");
      }

      const searchJson = await searchResp.json();
      if (searchJson.error) {
        throw new Error(searchJson.error || "Errore nella ricerca immagini");
      }

      // 3) Mostra i risultati nella UI
      const results = searchJson.results || [];
      setResults(Array.isArray(results) ? results : []);

      // 4) PREPARA il riassunto da mandare al modello
      const summaryParts = results.slice(0, 3).map((r: SearchResult, idx: number) => {
        const props = r.properties || {};
        const name = props.name || "(senza nome)";
        const pdf = props.source_pdf || "(sorgente sconosciuta)";
        const page = props.page_index ?? "?";
        const mediaType = props.mediaType || "";
        return `${idx + 1}. ${name} [${pdf} - pag. ${page}] ${mediaType}`;
      });

      const resultsSummary =
        results.length === 0
          ? "Nessun risultato trovato."
          : `Ho trovato ${results.length} risultati simili. I primi sono:\n` +
            summaryParts.join("\n");

      // 5) CHIAMATA al tool sinde_widget_push_results
      if (client) {
        try {
          await client.tools.call({
            name: "sinde_widget_push_results",
            arguments: {
              results_summary: resultsSummary,
              raw_results: searchJson, // oppure results
            },
          });
          console.log("✅ Risultati inviati al modello tramite sinde_widget_push_results");
          setStatus(`Ricerca completata. ${results.length} risultati trovati e inviati a ChatGPT.`);
        } catch (err: any) {
          console.error("Errore chiamando sinde_widget_push_results:", err);
          // NON bloccare la UI: al massimo mostri un warning
          setStatus(`Ricerca completata. ${results.length} risultati trovati (errore invio a ChatGPT: ${err.message || "errore sconosciuto"})`);
        }
      } else {
        console.warn("⚠️ Client OpenAI non disponibile, skip push risultati");
        setStatus(`Ricerca completata. ${results.length} risultati trovati (non inviati a ChatGPT - API non disponibile)`);
      }
    } catch (err: any) {
      console.error(err);
      setStatus(`Errore: ${err?.message || String(err)}`);
      setResults(null);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div
      style={{
        border: "1px solid #ddd",
        borderRadius: 8,
        padding: 12,
        fontSize: 14,
      }}
    >
      <div style={{ marginBottom: 8 }}>
        <input type="file" accept="image/*" onChange={handleFileChange} />
        <button
          onClick={handleUploadAndSearch}
          disabled={!file || isLoading}
          style={{
            marginLeft: 8,
            padding: "6px 12px",
            borderRadius: 6,
            border: "1px solid #ccc",
            cursor: !file || isLoading ? "default" : "pointer",
          }}
        >
          {isLoading ? "Attendere..." : "Carica e cerca"}
        </button>
      </div>

      {status && <p style={{ marginTop: 4 }}>{status}</p>}

      {results && (
        <div style={{ marginTop: 12 }}>
          <h3 style={{ marginTop: 0, fontSize: 15 }}>Risultati</h3>
          {results.length === 0 && <p>Nessun risultato trovato.</p>}
          {results.length > 0 && (
            <ul style={{ paddingLeft: 18 }}>
              {results.map((r, idx) => (
                <li key={idx} style={{ marginBottom: 6 }}>
                  <div>
                    <code>{r.uuid}</code>
                  </div>
                  {r.properties?.name && (
                    <div>Nome: {r.properties.name}</div>
                  )}
                  {typeof r.distance === "number" && (
                    <div>Distanza: {r.distance.toFixed(3)}</div>
                  )}
                  {r.properties?.source_pdf && (
                    <div>PDF: {r.properties.source_pdf}</div>
                  )}
                  {typeof r.properties?.page_index === "number" && (
                    <div>Pagina: {r.properties.page_index}</div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
};
