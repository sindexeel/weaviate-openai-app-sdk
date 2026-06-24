// src/ImageSearchWidget.tsx
import React, { useState, useEffect } from "react";

// URL base del tuo server MCP (quello con serve.py)
const MCP_BASE_URL = "https://weaviate-openai-app-sdk.onrender.com";

const DEBUG_MODE = true;

const ACCEPTED_TYPES = ".png,.jpg,.jpeg,.gif,.webp,.bmp,.tiff,.pdf,.dxf";

type SearchResult = {
  uuid?: string;
  properties?: {
    name?: string;
    source_pdf?: string;
    page_index?: number;
    mediaType?: string;
    image_b64?: string;
    [key: string]: any;
  };
  distance?: number;
  bm25_score?: number;
};

export const ImageSearchWidget: React.FC = () => {
  const [file, setFile] = useState<File | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [results, setResults] = useState<SearchResult[] | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [debugMode, setDebugMode] = useState(false);
  const [pdfPageCount, setPdfPageCount] = useState<number | null>(null);
  const [enlargedImage, setEnlargedImage] = useState<{
    src: string;
    alt: string;
  } | null>(null);

  // Chiudi il modal con ESC
  useEffect(() => {
    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === "Escape" && enlargedImage) {
        setEnlargedImage(null);
      }
    };
    window.addEventListener("keydown", handleEscape);
    return () => window.removeEventListener("keydown", handleEscape);
  }, [enlargedImage]);

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0] ?? null;
    setFile(f);
    setResults(null);
    setStatus(null);
    setPdfPageCount(null);
  };

  const handleUploadAndSearch = async () => {
    if (!file) {
      setStatus("Seleziona prima un progetto.");
      return;
    }

    try {
      setIsLoading(true);

      const ext = file.name.split(".").pop()?.toLowerCase() || "";
      const isPdf = ext === "pdf";
      const isDxf = ext === "dxf";
      const fileLabel = isPdf ? "PDF" : isDxf ? "DXF" : "progetto";

      setStatus(`Caricamento ${fileLabel} in corso${isPdf ? " (conversione pagine)..." : "..."}`);

      // 1) Upload file al backend /upload-image (gestisce immagini, PDF, DXF)
      const form = new FormData();
      form.append("image", file);

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

      // Per PDF multi-pagina il backend ritorna image_ids[]
      const imageIds: string[] = uploadData.image_ids || (uploadData.image_id ? [uploadData.image_id] : []);
      if (imageIds.length === 0) {
        throw new Error("Risposta /upload-image senza image_id");
      }

      if (isPdf && uploadData.pages > 1) {
        setPdfPageCount(uploadData.pages);
        setStatus(`PDF convertito: ${uploadData.pages} pagine. Ricerca in corso (pagina 1/${uploadData.pages})...`);
      } else {
        setStatus(`${fileLabel} caricato. Avvio la ricerca tra i progetti Sinde...`);
      }

      // 2) Cerca per ogni pagina (PDF) o singola immagine
      let allResults: SearchResult[] = [];

      for (let i = 0; i < imageIds.length; i++) {
        if (imageIds.length > 1) {
          setStatus(`Ricerca in corso (pagina ${i + 1}/${imageIds.length})...`);
        }

        const searchResp = await fetch(`${MCP_BASE_URL}/image-search`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            collection: "Sinde",
            image_id: imageIds[i],
            limit: 10,
          }),
        });

        if (!searchResp.ok) {
          const err = await searchResp.json().catch(() => ({}));
          throw new Error(err.error || `Errore nella ricerca (pagina ${i + 1})`);
        }

        const searchJson = await searchResp.json();
        if (searchJson.error) {
          throw new Error(searchJson.error || `Errore nella ricerca (pagina ${i + 1})`);
        }

        const pageResults = searchJson.results || [];
        allResults = allResults.concat(pageResults);
      }

      // Deduplica per uuid e ordina per distanza
      const seen = new Set<string>();
      const dedupResults = allResults.filter((r) => {
        const id = r.uuid || JSON.stringify(r.properties);
        if (seen.has(id)) return false;
        seen.add(id);
        return true;
      });
      dedupResults.sort((a, b) => (a.distance ?? 1) - (b.distance ?? 1));
      const results = dedupResults.slice(0, 10);

      setResults(Array.isArray(results) ? results : []);

      // 3) PREPARA il riassunto da mandare al modello
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

      // 4) Invia i risultati al backend MCP via HTTP
      try {
        const resp = await fetch(`${MCP_BASE_URL}/widget-push-results`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            results_summary: resultsSummary,
            raw_results: { count: results.length, results },
          }),
        });

        if (!resp.ok) {
          const errJson = await resp.json().catch(() => ({}));
          console.error("Errore /widget-push-results:", errJson);
          setStatus(
            `Ricerca completata. ${results.length} progetti trovati (errore salvataggio per ChatGPT)`
          );
        } else {
          console.log("Risultati salvati lato server per ChatGPT");
          setStatus(
            `Ricerca completata. ${results.length} progetti trovati.`
          );
        }
      } catch (err: any) {
        console.error("Errore chiamando /widget-push-results:", err);
        setStatus(
          `Ricerca completata. ${results.length} progetti trovati (errore integrazione: ${
            err?.message || "errore sconosciuto"
          })`
        );
      }
    } catch (err: any) {
      console.error(err);
      setStatus(`Errore: ${err?.message || String(err)}`);
      setResults(null);
    } finally {
      setIsLoading(false);
    }
  };

  const getStatusClass = (): string => {
    if (!status) return "status";
    if (status.includes("Errore")) return "status status--error";
    if (status.includes("completata")) return "status status--success";
    return "status status--info";
  };

  return (
    <div className="widget-root">
      {/* Header */}
      <div className="widget-header">
        <h1 className="widget-title">Ricerca progetti Sinde</h1>
        <p className="widget-subtitle">
          Carica un progetto (immagine, PDF o DXF) per trovare progetti simili nella collezione Sinde
        </p>
        {DEBUG_MODE && (
          <button
            onClick={() => setDebugMode((d) => !d)}
            title={debugMode ? "Disattiva modalita debug" : "Attiva modalita debug"}
            className={`debug-btn${debugMode ? " debug-btn--active" : ""}`}
          >
            {debugMode ? "DEBUG ON" : "DEBUG"}
          </button>
        )}
      </div>

      {/* Upload Section */}
      <div className="upload-section">
        <div className="upload-actions">
          <input
            type="file"
            accept={ACCEPTED_TYPES}
            onChange={handleFileChange}
            id="file-input"
            className="file-input-hidden"
          />
          <label htmlFor="file-input" className="btn-select-file">
            {file ? "Cambia progetto" : "Seleziona progetto"}
          </label>
        </div>
        {file && (
          <div className="file-selected">
            File selezionato: <strong>{file.name}</strong>
            {pdfPageCount && (
              <span className="file-selected-pages">
                ({pdfPageCount} pagine)
              </span>
            )}
          </div>
        )}
        <button
          onClick={handleUploadAndSearch}
          disabled={!file || isLoading}
          className="btn-search"
        >
          {isLoading ? "Ricerca in corso..." : "Cerca progetti simili"}
        </button>
      </div>

      {/* Status */}
      {status && <div className={getStatusClass()}>{status}</div>}

      {/* Results Grid */}
      {results && results.length > 0 && (
        <div className="results-section">
          <h2 className="results-title">Progetti trovati ({results.length})</h2>
          <div className="results-grid">
            {results.map((r, idx) => (
              <div key={idx} className="result-card">
                <div className="result-index">#{idx + 1}</div>

                {/* Anteprima immagine da image_b64 */}
                {r.properties?.image_b64 && (
                  <div
                    className="result-preview"
                    onClick={() => {
                      if (r.properties?.image_b64) {
                        setEnlargedImage({
                          src: `data:image/png;base64,${r.properties.image_b64}`,
                          alt: r.properties?.name || `Anteprima pagina ${r.properties?.page_index || ""}`,
                        });
                      }
                    }}
                  >
                    <img
                      src={`data:image/png;base64,${r.properties.image_b64}`}
                      alt={r.properties?.name || `Anteprima pagina ${r.properties?.page_index || ""}`}
                      onError={(e) => {
                        const parent = e.currentTarget.parentElement;
                        if (parent) parent.style.display = "none";
                      }}
                    />
                    <div className="preview-zoom-icon">🔍</div>
                  </div>
                )}

                {r.properties?.name && (
                  <h3 className="result-name">{r.properties.name}</h3>
                )}
                <div className="result-details">
                  {r.properties?.source_pdf && (
                    <div className="result-detail-row">
                      <strong>PDF:</strong> {r.properties.source_pdf}
                    </div>
                  )}
                  {typeof r.properties?.page_index === "number" && (
                    <div className="result-detail-row">
                      <strong>Pagina:</strong> {r.properties.page_index}
                    </div>
                  )}
                  {r.properties?.mediaType && (
                    <div className="result-detail-row">
                      <strong>Tipo:</strong> {r.properties.mediaType}
                    </div>
                  )}
                  {debugMode ? (
                    <div className="debug-panel">
                      <div className="debug-panel-title">DEBUG</div>
                      <div><strong>uuid:</strong> {r.uuid ?? "---"}</div>
                      {typeof r.distance === "number" && (
                        <>
                          <div><strong>distance:</strong> {r.distance.toFixed(6)}</div>
                          <div><strong>similarity (1-d):</strong> {(1 - r.distance).toFixed(6)}</div>
                        </>
                      )}
                      {typeof r.bm25_score === "number" && (
                        <div><strong>bm25_score:</strong> {r.bm25_score.toFixed(6)}</div>
                      )}
                    </div>
                  ) : (
                    typeof r.distance === "number" && (
                      <div className="similarity-badge">
                        <strong>Similarita:</strong> {(1 - r.distance).toFixed(3)}
                      </div>
                    )
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {results && results.length === 0 && (
        <div className="empty-results">Nessun progetto trovato.</div>
      )}

      {/* Modal per immagine ingrandita */}
      {enlargedImage && (
        <div className="modal-overlay" onClick={() => setEnlargedImage(null)}>
          <button
            onClick={(e) => {
              e.stopPropagation();
              setEnlargedImage(null);
            }}
            className="modal-close"
            aria-label="Chiudi"
          >
            x
          </button>
          <img
            src={enlargedImage.src}
            alt={enlargedImage.alt}
            className="modal-image"
            onClick={(e) => e.stopPropagation()}
          />
        </div>
      )}
    </div>
  );
};

export {};
