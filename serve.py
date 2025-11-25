# serve.py
import os
import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

# --- Weaviate client imports (v4) ---
import weaviate
from weaviate.classes.init import Auth
from weaviate.classes.query import MetadataQuery

# In-memory stato Vertex
_VERTEX_HEADERS: Dict[str, str] = {}
_VERTEX_REFRESH_THREAD_STARTED = False
_VERTEX_USER_PROJECT: Optional[str] = None

# In-memory storage per immagini caricate (temporaneo, scade dopo 1 ora)
_UPLOADED_IMAGES: Dict[str, Dict[str, Any]] = {}

_BASE_DIR = Path(__file__).resolve().parent
_DEFAULT_PROMPT_PATH = _BASE_DIR / "prompts" / "instructions.md"
_DEFAULT_DESCRIPTION_PATH = _BASE_DIR / "prompts" / "description.txt"
_WIDGET_DIST_DIR = _BASE_DIR / "weaviate-image-app" / "dist"
_BASE_URL = os.environ.get("BASE_URL", "https://weaviate-openai-app-sdk.onrender.com")


def _build_vertex_header_map(token: str) -> Dict[str, str]:
    headers: Dict[str, str] = {
        "X-Goog-Vertex-Api-Key": token,
    }
        # user-project opzionale
    if _VERTEX_USER_PROJECT:
        headers["X-Goog-User-Project"] = _VERTEX_USER_PROJECT
    return headers


def _discover_gcp_project() -> Optional[str]:
    gac_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if gac_json:
        try:
            data = json.loads(gac_json)
            if isinstance(data, dict) and data.get("project_id"):
                return data["project_id"]
        except Exception:
            pass

    gac_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if gac_path and os.path.exists(gac_path):
        try:
            with open(gac_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("project_id"):
                return data["project_id"]
        except Exception:
            pass

    try:
        import google.auth

        creds, proj = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        if proj:
            return proj
    except Exception:
        pass
    return None


def _get_weaviate_url() -> str:
    url = os.environ.get("WEAVIATE_CLUSTER_URL") or os.environ.get("WEAVIATE_URL")
    if not url:
        raise RuntimeError("Please set WEAVIATE_URL or WEAVIATE_CLUSTER_URL.")
    return url


def _get_weaviate_api_key() -> str:
    api_key = os.environ.get("WEAVIATE_API_KEY")
    if not api_key:
        raise RuntimeError("Please set WEAVIATE_API_KEY.")
    return api_key


def _resolve_service_account_path() -> Optional[str]:
    gac_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if gac_path and os.path.exists(gac_path):
        _load_vertex_user_project(gac_path)
        return gac_path

    candidates = [
        os.environ.get("VERTEX_SA_PATH"),
        "/etc/secrets/weaviate-sa.json",
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = candidate
            _load_vertex_user_project(candidate)
            return candidate
    return None


def _load_vertex_user_project(path: str) -> None:
    global _VERTEX_USER_PROJECT
    if _VERTEX_USER_PROJECT:
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _VERTEX_USER_PROJECT = data.get("project_id")
        if not _VERTEX_USER_PROJECT and data.get("quota_project_id"):
            _VERTEX_USER_PROJECT = data["quota_project_id"]
        if _VERTEX_USER_PROJECT:
            try:
                print(
                    f"[vertex-oauth] detected service account project: {_VERTEX_USER_PROJECT}"
                )
            except (ValueError, OSError):
                pass
        else:
            try:
                print(
                    "[vertex-oauth] warning: project_id not found in service account JSON"
                )
            except (ValueError, OSError):
                pass
    except Exception as exc:
        try:
            print(f"[vertex-oauth] unable to read project id from SA: {exc}")
        except (ValueError, OSError):
            pass


def _sync_refresh_vertex_token() -> bool:
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
    except Exception as exc:
        print(f"[vertex-oauth] sync refresh unavailable: {exc}")
        return False

    cred_path = _resolve_service_account_path()
    if not cred_path or not os.path.exists(cred_path):
        return False
    try:
        creds = service_account.Credentials.from_service_account_file(
            cred_path,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        creds.refresh(Request())
    except Exception as exc:
        print(f"[vertex-oauth] sync refresh error: {exc}")
        return False

    token = creds.token
    if not token:
        return False
    global _VERTEX_HEADERS
    _VERTEX_HEADERS = _build_vertex_header_map(token)
    print(f"[vertex-oauth] sync token refresh (prefix: {token[:10]}...)")
    if os.environ.get("GOOGLE_APIKEY") == token:
        os.environ.pop("GOOGLE_APIKEY", None)
    if os.environ.get("PALM_APIKEY") == token:
        os.environ.pop("PALM_APIKEY", None)
    return True


def _connect():
    url = _get_weaviate_url()
    key = _get_weaviate_api_key()
    _resolve_service_account_path()

    headers: Dict[str, str] = {}
    openai_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_APIKEY")
    if openai_key:
        headers["X-OpenAI-Api-Key"] = openai_key

    vertex_key = os.environ.get("VERTEX_APIKEY")
    vertex_bearer = os.environ.get("VERTEX_BEARER_TOKEN")

    if vertex_bearer:
        headers.update(_build_vertex_header_map(vertex_bearer))
        print("[vertex-oauth] using bearer token from VERTEX_BEARER_TOKEN env")

    if vertex_key and not headers:
        for k in [
            "X-Goog-Vertex-Api-Key",
            "X-Goog-Api-Key",
            "X-Palm-Api-Key",
            "X-Goog-Studio-Api-Key",
        ]:
            headers[k] = vertex_key
        print("[vertex-oauth] using static Vertex API key from VERTEX_APIKEY")

    if not headers and "_VERTEX_HEADERS" in globals() and _VERTEX_HEADERS:
        headers.update(_VERTEX_HEADERS)
    elif not headers:
        if _sync_refresh_vertex_token():
            headers.update(_VERTEX_HEADERS)
            token = _VERTEX_HEADERS.get("X-Goog-Vertex-Api-Key")
            if token:
                if os.environ.get("GOOGLE_APIKEY") == token:
                    os.environ.pop("GOOGLE_APIKEY", None)
                if os.environ.get("PALM_APIKEY") == token:
                    os.environ.pop("PALM_APIKEY", None)
        else:
            print("[vertex-oauth] unable to obtain Vertex token synchronously")

    vertex_token = headers.get("X-Goog-Vertex-Api-Key")
    if vertex_token:
        token_preview = vertex_token[:10]
        project_debug = headers.get("X-Goog-User-Project")
        if project_debug:
            print(
                f"[vertex-oauth] using Vertex header token prefix: {token_preview}... project: {project_debug}"
            )
        else:
            print(
                f"[vertex-oauth] using Vertex header token prefix: {token_preview}... (no x-goog-user-project)"
            )
    elif headers:
        print("[vertex-oauth] custom headers configured (non-Vertex)")
    else:
        print("[vertex-oauth] WARNING: no Vertex headers available for connection")

    client = weaviate.connect_to_weaviate_cloud(
        cluster_url=url,
        auth_credentials=Auth.api_key(key),
        headers=headers or None,
    )

    grpc_meta: Dict[str, str] = {}
    for k, v in (headers or {}).items():
        kk = k.lower()
        if kk not in {"x-goog-vertex-api-key", "x-goog-user-project"}:
            continue
        grpc_meta[kk] = v

    if vertex_key:
        for kk in [
            "x-goog-vertex-api-key",
            "x-goog-api-key",
            "x-palm-api-key",
            "x-goog-studio-api-key",
        ]:
            grpc_meta.setdefault(kk, vertex_key)
    else:
        if "authorization" not in grpc_meta and "_VERTEX_HEADERS" in globals() and _VERTEX_HEADERS:
            auth = _VERTEX_HEADERS.get("Authorization") or _VERTEX_HEADERS.get(
                "authorization"
            )
            if auth:
                grpc_meta["authorization"] = auth

    if openai_key:
        grpc_meta["x-openai-api-key"] = openai_key

    try:
        conn = getattr(client, "_connection", None)
        if conn is not None:
            meta_list = list(grpc_meta.items())
            try:
                setattr(conn, "grpc_metadata", meta_list)
            except Exception:
                pass
            try:
                setattr(conn, "_grpc_metadata", meta_list)
            except Exception:
                pass
            if hasattr(conn, "set_grpc_metadata"):
                try:
                    conn.set_grpc_metadata(meta_list)
                except Exception:
                    pass
            debug_meta = getattr(conn, "grpc_metadata", None)
            print(f"[vertex-oauth] grpc metadata now: {debug_meta}")
    except Exception as e:
        print("[weaviate] warning: cannot set gRPC metadata headers:", e)

    return client


def _load_text_source(env_keys, file_path):
    if isinstance(env_keys, str):
        env_keys = [env_keys]
    path = Path(file_path) if file_path else None
    if path and path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception as exc:
            print(f"[mcp] warning: cannot read instructions file '{path}': {exc}")
    for key in env_keys:
        val = os.environ.get(key)
        if val:
            return val.strip()
    return None


_MCP_SERVER_NAME = os.environ.get("MCP_SERVER_NAME", "weaviate-mcp-http")
_MCP_INSTRUCTIONS_FILE = os.environ.get("MCP_PROMPT_FILE") or os.environ.get(
    "MCP_INSTRUCTIONS_FILE"
)
if not _MCP_INSTRUCTIONS_FILE and _DEFAULT_PROMPT_PATH.exists():
    _MCP_INSTRUCTIONS_FILE = str(_DEFAULT_PROMPT_PATH)
_MCP_DESCRIPTION_FILE = os.environ.get("MCP_DESCRIPTION_FILE")
if not _MCP_DESCRIPTION_FILE and _DEFAULT_DESCRIPTION_PATH.exists():
    _MCP_DESCRIPTION_FILE = str(_DEFAULT_DESCRIPTION_PATH)

_MCP_INSTRUCTIONS = _load_text_source(
    ["MCP_PROMPT", "MCP_INSTRUCTIONS"], _MCP_INSTRUCTIONS_FILE
)
_MCP_DESCRIPTION = _load_text_source("MCP_DESCRIPTION", _MCP_DESCRIPTION_FILE)

# Porta e host per FastMCP / uvicorn (per Render)
SERVER_PORT = int(os.environ.get("PORT", "10000"))
os.environ.setdefault("FASTMCP_PORT", str(SERVER_PORT))
os.environ.setdefault("FASTMCP_HOST", "0.0.0.0")

# Non passiamo host/port direttamente, lasciamo che FastMCP usi le env FASTMCP_*
mcp = FastMCP(_MCP_SERVER_NAME)


def _apply_mcp_metadata():
    try:
        if hasattr(mcp, "set_server_info"):
            server_info: Dict[str, Any] = {}
            if _MCP_DESCRIPTION:
                server_info["description"] = _MCP_DESCRIPTION
            if _MCP_INSTRUCTIONS:
                server_info["instructions"] = _MCP_INSTRUCTIONS
            if server_info:
                mcp.set_server_info(**server_info)
    except Exception:
        pass


_apply_mcp_metadata()


def _load_widget_html() -> str:
    widget_html_path = _WIDGET_DIST_DIR / "index.html"

    if not widget_html_path.exists():
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Image Search Widget</title>
</head>
<body>
  <div id="root"></div>
  <script type="module" src="{_BASE_URL}/assets/index.js"></script>
</body>
</html>"""

    try:
        with open(widget_html_path, "r", encoding="utf-8") as f:
            html_content = f.read()

        html_content = html_content.replace(
            'src="/assets/', f'src="{_BASE_URL}/assets/'
        )
        html_content = html_content.replace(
            'href="/assets/', f'href="{_BASE_URL}/assets/'
        )
        html_content = html_content.replace(
            'src="assets/', f'src="{_BASE_URL}/assets/'
        )
        html_content = html_content.replace(
            'href="assets/', f'href="{_BASE_URL}/assets/'
        )

        return html_content
    except Exception as e:
        print(f"[widget] Error loading widget HTML: {e}")
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Image Search Widget</title>
</head>
<body>
  <div id="root"></div>
  <script type="module" src="{_BASE_URL}/assets/index.js"></script>
</body>
</html>"""


widget_uri = "ui://widget/image-search.html"


@mcp.resource(
    uri=widget_uri,
    name="image-search-widget",
    description="Widget per la ricerca di immagini in Weaviate",
)
def image_search_widget_resource():
    widget_html = _load_widget_html()
    return {
        "contents": [
            {
                "uri": widget_uri,
                "mimeType": "text/html+skybridge",
                "text": widget_html,
                "_meta": {
                    "openai/widgetPrefersBorder": True,
                    "openai/widgetDomain": "https://chatgpt.com",
                    "openai/widgetCSP": {
                        "connect_domains": [_BASE_URL],
                        "resource_domains": ["https://*.oaistatic.com"],
                    },
                },
            }
        ],
    }


@mcp.tool()
def open_image_search_widget() -> Dict[str, Any]:
    """
    Apre il widget interattivo per la ricerca di immagini.
    """
    return {
        "structuredContent": {
            "widgetReady": True,
            "message": "Widget di ricerca immagini pronto all'uso.",
        },
        "content": [
            {
                "type": "text",
                "text": (
                    "Ho aperto il widget di ricerca immagini. "
                    "Puoi caricare un'immagine e cercare immagini simili nella collection Sinde."
                ),
            }
        ],
        "_meta": {
            "baseUrl": _BASE_URL,
        },
    }


def _add_tool_metadata():
    try:
        if hasattr(mcp, "_tools"):
            tools = mcp._tools
        elif hasattr(mcp, "tools"):
            tools = mcp.tools
        else:
            app = getattr(mcp, "app", None) or getattr(mcp, "_app", None)
            if app and hasattr(app, "state") and hasattr(app.state, "tools"):
                tools = app.state.tools
            else:
                return

        tool_name = "open_image_search_widget"
        if isinstance(tools, dict) and tool_name in tools:
            tool_def = tools[tool_name]
            meta = getattr(tool_def, "_meta", None)
            if not isinstance(meta, dict):
                meta = {}
            meta.update(
                {
                    "openai/outputTemplate": widget_uri,
                    "openai/toolInvocation/invoking": (
                        "Aprendo il widget di ricerca immagini..."
                    ),
                    "openai/toolInvocation/invoked": (
                        "Widget di ricerca immagini pronto."
                    ),
                }
            )
            tool_def._meta = meta
        elif isinstance(tools, list):
            for tool_def in tools:
                if getattr(tool_def, "name", None) == tool_name:
                    meta = getattr(tool_def, "_meta", None)
                    if not isinstance(meta, dict):
                        meta = {}
                    meta.update(
                        {
                            "openai/outputTemplate": widget_uri,
                            "openai/toolInvocation/invoking": (
                                "Aprendo il widget di ricerca immagini..."
                            ),
                            "openai/toolInvocation/invoked": (
                                "Widget di ricerca immagini pronto."
                            ),
                        }
                    )
                    tool_def._meta = meta
                    break
    except Exception as e:
        print(f"[widget] Warning: Could not add metadata to tool: {e}")


_add_tool_metadata()


@mcp.custom_route("/health", methods=["GET"])
async def health(_request):
    return JSONResponse({"status": "ok", "service": "weaviate-mcp-http"})


@mcp.custom_route("/assets/{file_path:path}", methods=["GET"])
async def serve_assets(request):
    from starlette.responses import FileResponse

    file_path = request.path_params.get("file_path", "")

    full_path = _WIDGET_DIST_DIR / "assets" / file_path
    if not full_path.exists():
        full_path = _WIDGET_DIST_DIR / file_path

    try:
        resolved_path = full_path.resolve()
        dist_resolved = _WIDGET_DIST_DIR.resolve()
        resolved_path.relative_to(dist_resolved)
    except (ValueError, OSError):
        return JSONResponse({"error": "Invalid path"}, status_code=400)

    if not full_path.exists() or not full_path.is_file():
        return JSONResponse({"error": "Not found"}, status_code=404)

    content_type_map = {
        ".js": "application/javascript",
        ".mjs": "application/javascript",
        ".css": "text/css",
        ".html": "text/html",
        ".json": "application/json",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
        ".webp": "image/webp",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
        ".ttf": "font/ttf",
        ".eot": "application/vnd.ms-fontobject",
    }

    ext = full_path.suffix.lower()
    content_type = content_type_map.get(ext, "application/octet-stream")

    return FileResponse(
        full_path,
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=31536000",
            "Access-Control-Allow-Origin": "*",
        },
    )


@mcp.custom_route("/upload-image", methods=["POST"])
async def upload_image_endpoint(request):
    """
    Endpoint HTTP per upload diretto di immagini.
    """
    try:
        content_type = request.headers.get("content-type", "")
        image_b64 = None

        if "multipart/form-data" in content_type:
            form = await request.form()
            if "image" not in form:
                return JSONResponse(
                    {"error": "Missing 'image' field in form data"}, status_code=400
                )

            file = form["image"]
            if hasattr(file, "read"):
                import base64

                file_bytes = await file.read()
                image_b64 = base64.b64encode(file_bytes).decode("utf-8")
            else:
                return JSONResponse(
                    {"error": "Invalid file upload"}, status_code=400
                )
        else:
            try:
                data = await request.json()
                image_b64 = data.get("image_b64")
                if not image_b64:
                    return JSONResponse(
                        {"error": "Missing 'image_b64' in JSON body"}, status_code=400
                    )
            except Exception:
                return JSONResponse(
                    {
                        "error": (
                            "Invalid request format. Use multipart/form-data with "
                            "'image' field or JSON with 'image_b64'"
                        )
                    },
                    status_code=400,
                )

        if not image_b64:
            return JSONResponse(
                {"error": "No image data provided"}, status_code=400
            )

        cleaned_b64 = _clean_base64(image_b64)
        if not cleaned_b64:
            return JSONResponse(
                {"error": "Invalid base64 image string"}, status_code=400
            )

        image_id = str(uuid.uuid4())

        _UPLOADED_IMAGES[image_id] = {
            "image_b64": cleaned_b64,
            "expires_at": time.time() + 3600,
        }

        current_time = time.time()
        expired_ids = [
            img_id
            for img_id, data in _UPLOADED_IMAGES.items()
            if data["expires_at"] < current_time
        ]
        for img_id in expired_ids:
            _UPLOADED_IMAGES.pop(img_id, None)

        return JSONResponse({"image_id": image_id, "expires_in": 3600})
    except Exception as e:
        print(f"[upload-image] error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.tool()
def get_instructions() -> Dict[str, Any]:
    return {
        "instructions": _MCP_INSTRUCTIONS,
        "description": _MCP_DESCRIPTION,
        "server_name": _MCP_SERVER_NAME,
        "prompt_file": _MCP_INSTRUCTIONS_FILE,
        "description_file": _MCP_DESCRIPTION_FILE,
    }


@mcp.tool()
def reload_instructions() -> Dict[str, Any]:
    global _MCP_INSTRUCTIONS, _MCP_DESCRIPTION, _MCP_INSTRUCTIONS_FILE, _MCP_DESCRIPTION_FILE
    _MCP_INSTRUCTIONS_FILE = os.environ.get("MCP_PROMPT_FILE") or os.environ.get(
        "MCP_INSTRUCTIONS_FILE"
    )
    if not _MCP_INSTRUCTIONS_FILE and _DEFAULT_PROMPT_PATH.exists():
        _MCP_INSTRUCTIONS_FILE = str(_DEFAULT_PROMPT_PATH)
    _MCP_DESCRIPTION_FILE = os.environ.get("MCP_DESCRIPTION_FILE")
    if not _MCP_DESCRIPTION_FILE and _DEFAULT_DESCRIPTION_PATH.exists():
        _MCP_DESCRIPTION_FILE = str(_DEFAULT_DESCRIPTION_PATH)
    _MCP_INSTRUCTIONS = _load_text_source(
        ["MCP_PROMPT", "MCP_INSTRUCTIONS"], _MCP_INSTRUCTIONS_FILE
    )
    _MCP_DESCRIPTION = _load_text_source("MCP_DESCRIPTION", _MCP_DESCRIPTION_FILE)
    _apply_mcp_metadata()
    return get_instructions()


@mcp.tool()
def get_config() -> Dict[str, Any]:
    return {
        "weaviate_url": os.environ.get("WEAVIATE_CLUSTER_URL")
        or os.environ.get("WEAVIATE_URL"),
        "weaviate_api_key_set": bool(os.environ.get("WEAVIATE_API_KEY")),
        "openai_api_key_set": bool(
            os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_APIKEY")
        ),
        "cohere_api_key_set": bool(os.environ.get("COHERE_API_KEY")),
    }


@mcp.tool()
def debug_widget() -> Dict[str, Any]:
    widget_html_path = _WIDGET_DIST_DIR / "index.html"
    widget_exists = widget_html_path.exists()
    assets_dir = _WIDGET_DIST_DIR / "assets"
    assets_exist = assets_dir.exists() if assets_dir else False

    return {
        "widget_html_exists": widget_exists,
        "widget_html_path": str(widget_html_path),
        "assets_dir_exists": assets_exist,
        "base_url": _BASE_URL,
        "widget_template_uri": widget_uri,
        "widget_identifier": "image-search-widget",
    }


@mcp.tool()
def check_connection() -> Dict[str, Any]:
    client = _connect()
    try:
        ready = client.is_ready()
        return {"ready": bool(ready)}
    finally:
        client.close()


@mcp.tool()
def upload_image(
    image_url: Optional[str] = None, image_path: Optional[str] = None
) -> Dict[str, Any]:
    global _UPLOADED_IMAGES

    cleaned_b64 = None

    if image_path:
        print(f"[upload_image] Loading image from path: {image_path}")
        try:
            import base64

            if not os.path.exists(image_path):
                return {"error": f"File not found: {image_path}"}
            with open(image_path, "rb") as f:
                file_bytes = f.read()
                image_b64_raw = base64.b64encode(file_bytes).decode("utf-8")
                cleaned_b64 = _clean_base64(image_b64_raw)
        except Exception as e:
            return {
                "error": f"Failed to load image from path {image_path}: {str(e)}"
            }
        if not cleaned_b64:
            return {"error": f"Invalid image file: {image_path}"}
    elif image_url:
        print(f"[upload_image] Loading image from URL: {image_url}")
        cleaned_b64 = _load_image_from_url(image_url)
        if not cleaned_b64:
            return {"error": f"Failed to load image from URL: {image_url}"}
    else:
        return {"error": "Either image_url or image_path must be provided"}

    image_id = str(uuid.uuid4())

    _UPLOADED_IMAGES[image_id] = {
        "image_b64": cleaned_b64,
        "expires_at": time.time() + 3600,
    }

    current_time = time.time()
    expired_ids = [
        img_id
        for img_id, data in _UPLOADED_IMAGES.items()
        if data["expires_at"] < current_time
    ]
    for img_id in expired_ids:
        _UPLOADED_IMAGES.pop(img_id, None)

    return {"image_id": image_id, "expires_in": 3600}


@mcp.tool()
def list_collections() -> List[str]:
    client = _connect()
    try:
        colls = client.collections.list_all()
        if isinstance(colls, dict):
            names = list(colls.keys())
        else:
            try:
                names = [getattr(c, "name", str(c)) for c in colls]
            except Exception:
                names = list(colls)
        return sorted(set(names))
    finally:
        client.close()


@mcp.tool()
def get_schema(collection: str) -> Dict[str, Any]:
    client = _connect()
    try:
        coll = client.collections.get(collection)
        if coll is None:
            return {"error": f"Collection '{collection}' not found"}
        try:
            cfg = coll.config.get()
        except Exception:
            try:
                cfg = coll.config.get_class()
            except Exception:
                cfg = {"info": "config API not available in this client version"}
        return {"collection": collection, "config": cfg}
    finally:
        client.close()


@mcp.tool()
def keyword_search(collection: str, query: str, limit: int = 10) -> Dict[str, Any]:
    client = _connect()
    try:
        coll = client.collections.get(collection)
        if coll is None:
            return {"error": f"Collection '{collection}' not found"}
        resp = coll.query.bm25(
            query=query,
            return_metadata=MetadataQuery(score=True),
            limit=limit,
        )
        out = []
        for o in getattr(resp, "objects", []) or []:
            out.append(
                {
                    "uuid": str(getattr(o, "uuid", "")),
                    "properties": getattr(o, "properties", {}),
                    "bm25_score": getattr(getattr(o, "metadata", None), "score", None),
                }
            )
        return {"count": len(out), "results": out}
    finally:
        client.close()


@mcp.tool()
def semantic_search(collection: str, query: str, limit: int = 10) -> Dict[str, Any]:
    client = _connect()
    try:
        coll = client.collections.get(collection)
        if coll is None:
            return {"error": f"Collection '{collection}' not found"}
        resp = coll.query.near_text(
            query=query,
            limit=limit,
            return_metadata=MetadataQuery(distance=True),
        )
        out = []
        for o in getattr(resp, "objects", []) or []:
            out.append(
                {
                    "uuid": str(getattr(o, "uuid", "")),
                    "properties": getattr(o, "properties", {}),
                    "distance": getattr(getattr(o, "metadata", None), "distance", None),
                }
            )
        return {"count": len(out), "results": out}
    finally:
        client.close()


@mcp.tool()
def hybrid_search(
    collection: str,
    query: str,
    limit: int = 10,
    alpha: float = 0.8,
    query_properties: Optional[Any] = None,
    image_id: Optional[str] = None,
    image_url: Optional[str] = None,
) -> Dict[str, Any]:
    if collection and collection != "Sinde":
        print(
            f"[hybrid_search] warning: collection '{collection}' requested, but using 'Sinde' as per instructions"
        )
        collection = "Sinde"

    if query_properties and isinstance(query_properties, str):
        try:
            query_properties = json.loads(query_properties)
        except (json.JSONDecodeError, TypeError):
            pass

    image_b64 = None

    if image_id:
        if image_id in _UPLOADED_IMAGES:
            img_data = _UPLOADED_IMAGES[image_id]
            if img_data["expires_at"] > time.time():
                image_b64 = img_data["image_b64"]
            else:
                _UPLOADED_IMAGES.pop(image_id, None)
                return {
                    "error": (
                        f"Image ID {image_id} has expired. Please upload the image again."
                    )
                }
        else:
            return {
                "error": (
                    f"Image ID {image_id} not found. Please upload the image first using upload_image."
                )
            }

    if image_url and not image_b64:
        image_b64 = _load_image_from_url(image_url)
        if not image_b64:
            return {"error": f"Failed to load image from URL: {image_url}"}
        image_b64 = _clean_base64(image_b64)
        if not image_b64:
            return {"error": f"Invalid image format from URL: {image_url}"}

    client = _connect()
    try:
        coll = client.collections.get(collection)
        if coll is None:
            return {"error": f"Collection '{collection}' not found"}

        if image_b64:
            vec = _vertex_embed(image_b64=image_b64, text=query if query else None)
            hybrid_params: Dict[str, Any] = {
                "query": query if query else "",
                "alpha": alpha,
                "limit": limit,
                "vector": vec,
                "return_properties": ["name", "source_pdf", "page_index", "mediaType"],
                "return_metadata": MetadataQuery(score=True, distance=True),
            }
            if query_properties:
                hybrid_params["query_properties"] = query_properties
            resp = coll.query.hybrid(**hybrid_params)
        else:
            hybrid_params = {
                "query": query,
                "alpha": alpha,
                "limit": limit,
                "return_properties": ["name", "source_pdf", "page_index", "mediaType"],
                "return_metadata": MetadataQuery(score=True, distance=True),
            }
            if query_properties:
                hybrid_params["query_properties"] = query_properties
            resp = coll.query.hybrid(**hybrid_params)

        out = []
        for o in getattr(resp, "objects", []) or []:
            md = getattr(o, "metadata", None)
            score = getattr(md, "score", None)
            distance = getattr(md, "distance", None)
            out.append(
                {
                    "uuid": str(getattr(o, "uuid", "")),
                    "properties": getattr(o, "properties", {}),
                    "bm25_score": score,
                    "distance": distance,
                }
            )
        return {"count": len(out), "results": out}
    finally:
        client.close()


try:
    from google.cloud import aiplatform

    _VERTEX_AVAILABLE = True
except Exception:
    _VERTEX_AVAILABLE = False


def _ensure_gcp_adc():
    gac_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    gac_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if gac_json and not gac_path:
        tmp_path = "/app/gcp_credentials.json"
        with open(tmp_path, "w", encoding="utf-8") as f2:
            f2.write(gac_json)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = tmp_path
    _resolve_service_account_path()
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        _load_vertex_user_project(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])


def _load_image_from_url(image_url: str) -> Optional[str]:
    try:
        import requests
        import base64

        response = requests.get(image_url, timeout=30, stream=True)
        response.raise_for_status()

        content_type = response.headers.get("content-type", "").lower()
        if not content_type.startswith("image/"):
            print(
                f"[image] warning: URL {image_url} does not return an image (content-type: {content_type})"
            )

        content = response.content
        if len(content) > 10 * 1024 * 1024:
            print(
                f"[image] warning: image from {image_url} is too large ({len(content)} bytes)"
            )
            return None

        if len(content) < 100:
            print(
                f"[image] warning: image from {image_url} is too small ({len(content)} bytes)"
            )
            return None

        valid_formats = {
            b"\xff\xd8\xff": "JPEG",
            b"\x89PNG\r\n\x1a\n": "PNG",
            b"GIF87a": "GIF",
            b"GIF89a": "GIF",
            b"RIFF": "WEBP",
        }
        is_valid = False
        for magic, fmt in valid_formats.items():
            if content.startswith(magic):
                is_valid = True
                print(f"[image] detected format: {fmt} from {image_url}")
                break

        if not is_valid:
            print(f"[image] warning: {image_url} may not be a valid image format")

        return base64.b64encode(content).decode("utf-8")
    except Exception as e:
        print(f"[image] error loading from URL {image_url}: {e}")
        return None


def _clean_base64(image_b64: str) -> Optional[str]:
    import base64
    import re

    if image_b64.startswith("data:"):
        match = re.match(r"data:image/[^;]+;base64,(.+)", image_b64)
        if match:
            image_b64 = match.group(1)
        else:
            return None

    image_b64 = image_b64.strip()

    try:
        if not re.match(r"^[A-Za-z0-9+/=]+$", image_b64):
            print("[image] invalid base64 characters")
            return None

        decoded = base64.b64decode(image_b64, validate=True)

        if len(decoded) == 0:
            print("[image] empty image data")
            return None

        if len(decoded) < 10:
            print(f"[image] image too small ({len(decoded)} bytes)")
            return None

        return image_b64
    except Exception as e:
        print(f"[image] base64 validation error: {e}")
        return None


def _vertex_embed(
    image_b64: Optional[str] = None,
    text: Optional[str] = None,
    model: str = "multimodalembedding@001",
):
    if not _VERTEX_AVAILABLE:
        raise RuntimeError("google-cloud-aiplatform not installed")
    project = _discover_gcp_project()
    location = os.environ.get("VERTEX_LOCATION", "us-central1")
    if not project:
        raise RuntimeError(
            "Cannot determine GCP project_id from credentials; set GOOGLE_APPLICATION_CREDENTIALS(_JSON)."
        )
    _ensure_gcp_adc()
    from vertexai.vision_models import MultiModalEmbeddingModel, Image

    mdl = MultiModalEmbeddingModel.from_pretrained(model)
    import base64

    image = None
    if image_b64:
        image_bytes = base64.b64decode(image_b64)
        image = Image(image_bytes)
    resp = mdl.get_embeddings(image=image, contextual_text=text)
    if getattr(resp, "image_embedding", None):
        return list(resp.image_embedding)
    if getattr(resp, "text_embedding", None):
        return list(resp.text_embedding)
    if getattr(resp, "embedding", None):
        return list(resp.embedding)
    raise RuntimeError("No embedding returned from Vertex AI")


@mcp.tool()
def insert_image_vertex(
    collection: str,
    image_id: Optional[str] = None,
    image_url: Optional[str] = None,
    caption: Optional[str] = None,
    id: Optional[str] = None,
) -> Dict[str, Any]:
    image_b64 = None

    if image_id:
        if image_id in _UPLOADED_IMAGES:
            img_data = _UPLOADED_IMAGES[image_id]
            if img_data["expires_at"] > time.time():
                image_b64 = img_data["image_b64"]
            else:
                _UPLOADED_IMAGES.pop(image_id, None)
                return {
                    "error": f"Image ID {image_id} has expired. Please upload the image again."
                }
        else:
            return {
                "error": (
                    f"Image ID {image_id} not found. Use upload_image or /upload-image first."
                )
            }

    if image_url and not image_b64:
        image_b64 = _load_image_from_url(image_url)
        if not image_b64:
            return {"error": f"Failed to load image from URL: {image_url}"}
        image_b64 = _clean_base64(image_b64)
        if not image_b64:
            return {"error": f"Invalid image format from URL: {image_url}"}

    if not image_b64:
        return {"error": "Either image_id or image_url must be provided"}

    vec = _vertex_embed(image_b64=image_b64, text=caption)
    client = _connect()
    try:
        coll = client.collections.get(collection)
        if coll is None:
            return {"error": f"Collection '{collection}' not found"}

        obj = coll.data.insert(
            properties={"caption": caption, "image_b64": image_b64},
            vectors={"image": vec},
        )
        return {
            "uuid": str(getattr(obj, "uuid", "")),
            "named_vector": "image",
        }
    finally:
        client.close()


@mcp.tool()
def image_search_vertex(
    collection: str,
    image_id: Optional[str] = None,
    image_url: Optional[str] = None,
    caption: Optional[str] = None,
    limit: int = 10,
) -> Dict[str, Any]:
    if collection and collection != "Sinde":
        print(
            f"[image_search_vertex] warning: collection '{collection}' requested, but using 'Sinde' as per instructions"
        )
        collection = "Sinde"

    image_b64 = None

    if image_id:
        if image_id in _UPLOADED_IMAGES:
            img_data = _UPLOADED_IMAGES[image_id]
            if img_data["expires_at"] > time.time():
                image_b64 = img_data["image_b64"]
            else:
                _UPLOADED_IMAGES.pop(image_id, None)
                return {
                    "error": f"Image ID {image_id} has expired. Please upload the image again."
                }
        else:
            return {
                "error": (
                    f"Image ID {image_id} not found. Please upload the image first using upload_image."
                )
            }

    if image_url and not image_b64:
        image_b64 = _load_image_from_url(image_url)
        if not image_b64:
            return {"error": f"Failed to load image from URL: {image_url}"}
        image_b64 = _clean_base64(image_b64)
        if not image_b64:
            return {"error": f"Invalid image format from URL: {image_url}"}

    if not image_b64:
        return {"error": "Either image_id or image_url must be provided"}

    client = _connect()
    try:
        coll = client.collections.get(collection)
        if coll is None:
            return {"error": f"Collection '{collection}' not found"}
        resp = coll.query.near_image(
            image_b64,
            limit=limit,
            return_properties=["name", "source_pdf", "page_index", "mediaType"],
            return_metadata=MetadataQuery(distance=True),
        )
        out = []
        for o in getattr(resp, "objects", []) or []:
            out.append(
                {
                    "uuid": str(getattr(o, "uuid", "")),
                    "properties": getattr(o, "properties", {}),
                    "distance": getattr(getattr(o, "metadata", None), "distance", None),
                }
            )
        return {"count": len(out), "results": out}
    finally:
        client.close()


@mcp.tool()
def diagnose_vertex() -> Dict[str, Any]:
    info: Dict[str, Any] = {}
    info["project_id"] = _discover_gcp_project()
    info["oauth_enabled"] = os.environ.get("VERTEX_USE_OAUTH", "").lower() in (
        "1",
        "true",
        "yes",
    )
    info["headers_active"] = bool(_VERTEX_HEADERS) if "_VERTEX_HEADERS" in globals() else False
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request

        SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
        gac_path = _resolve_service_account_path()
        token_preview = None
        expiry = None
        if gac_path and os.path.exists(gac_path):
            creds = service_account.Credentials.from_service_account_file(
                gac_path, scopes=SCOPES
            )
            creds.refresh(Request())
            token_preview = (creds.token[:12] + "...") if creds.token else None
            expiry = getattr(creds, "expiry", None)
        info["token_sample"] = token_preview
        info["token_expiry"] = str(expiry) if expiry else None
    except Exception as e:
        info["token_error"] = str(e)
    return info


# ==== Vertex OAuth Token Refresher (optional) ===============================
def _write_adc_from_json_env():
    gac_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    gac_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if gac_json and not gac_path:
        tmp_path = "/app/gcp_credentials.json"
        with open(tmp_path, "w", encoding="utf-8") as f2:
            f2.write(gac_json)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = tmp_path
    _resolve_service_account_path()


def _refresh_vertex_oauth_loop():
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request
    import datetime
    import time

    SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
    cred_path = _resolve_service_account_path()
    if not cred_path or not os.path.exists(cred_path):
        print("[vertex-oauth] GOOGLE_APPLICATION_CREDENTIALS missing; token refresher disabled")
        return
    creds = service_account.Credentials.from_service_account_file(
        cred_path, scopes=SCOPES
    )
    global _VERTEX_HEADERS
    while True:
        try:
            creds.refresh(Request())
            token = creds.token
            _VERTEX_HEADERS = _build_vertex_header_map(token)
            if os.environ.get("GOOGLE_APIKEY") == token:
                os.environ.pop("GOOGLE_APIKEY", None)
            if os.environ.get("PALM_APIKEY") == token:
                os.environ.pop("PALM_APIKEY", None)
            token_preview = token[:10] if token else None
            print(f"[vertex-oauth] 🔄 Vertex token refreshed (prefix: {token_preview}...)")
            sleep_s = 55 * 60
            if creds.expiry:
                now = datetime.datetime.utcnow().replace(tzinfo=creds.expiry.tzinfo)
                delta = (creds.expiry - now).total_seconds() - 300
                if delta > 300:
                    sleep_s = int(delta)
            time.sleep(sleep_s)
        except Exception as e:
            print(f"[vertex-oauth] refresh error: {e}")
            time.sleep(60)


def _maybe_start_vertex_oauth_refresher():
    global _VERTEX_REFRESH_THREAD_STARTED
    if _VERTEX_REFRESH_THREAD_STARTED:
        return
    if os.environ.get("VERTEX_USE_OAUTH", "").lower() not in ("1", "true", "yes"):
        return
    _write_adc_from_json_env()
    sa_path = _resolve_service_account_path()
    if not sa_path:
        print("[vertex-oauth] service account path not found; refresher not started")
        return
    import threading

    t = threading.Thread(target=_refresh_vertex_oauth_loop, daemon=True)
    t.start()
    _VERTEX_REFRESH_THREAD_STARTED = True


_maybe_start_vertex_oauth_refresher()

# --- Alias /mcp senza slash finale, se serve --------------------------------
try:
    from starlette.routing import Route

    _starlette_app = getattr(mcp, "app", None) or getattr(mcp, "_app", None)

    if _starlette_app is not None:

        async def _mcp_alias(request):
            scope = dict(request.scope)
            scope["path"] = "/mcp/"
            scope["raw_path"] = b"/mcp/"
            return await _starlette_app(scope, request.receive, request.send)

        _starlette_app.router.routes.insert(
            0,
            Route(
                "/mcp",
                endpoint=_mcp_alias,
                methods=["GET", "HEAD", "POST", "OPTIONS"],
            ),
        )
except Exception as _route_err:
    print("[mcp] warning: cannot register MCP alias route:", _route_err)

# ==== main: avvia il server MCP in modalità streamable-http ==================
if __name__ == "__main__":
    import inspect

    # Porta/host che Render si aspetta
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", "10000"))

    # Path MCP (come avevi già prima)
    raw_path = os.environ.get("MCP_PATH", "/mcp")
    if not raw_path.startswith("/"):
        raw_path = "/" + raw_path
    path = raw_path.rstrip("/") or "/"

    # Prova prima il transport streamable-http con host/port/path
    try:
        sig = inspect.signature(mcp.run)
        params = sig.parameters

        kwargs = {"transport": "streamable-http"}
        if "host" in params:
            kwargs["host"] = host
        if "port" in params:
            kwargs["port"] = port
        if "path" in params:
            kwargs["path"] = path

        print(f"[mcp] starting streamable-http server with kwargs: {kwargs}")
        mcp.run(**kwargs)

    except (TypeError, ValueError) as e:
        # Se questa versione di FastMCP non supporta host/port/path,
        # fai fallback su streamable-http e prova a pilotarla via env
        print(f"[mcp] streamable-http run() with host/port/path failed: {e}")
        os.environ.setdefault("FASTMCP_HOST", host)
        os.environ.setdefault("FASTMCP_PORT", str(port))
        print("[mcp] falling back to streamable-http on 0.0.0.0:$PORT")
        try:
            mcp.run(transport="streamable-http")
        except Exception as e2:
            # Fallback finale: usa uvicorn direttamente
            print(f"[mcp] streamable-http also failed: {e2}")
            try:
                import uvicorn
                app = getattr(mcp, "app", None) or getattr(mcp, "_app", None)
                if app:
                    print(f"[mcp] using uvicorn fallback on {host}:{port}")
                    uvicorn.run(app, host=host, port=port, log_level="info")
                else:
                    raise RuntimeError("Cannot find FastMCP app")
            except ImportError:
                raise RuntimeError(f"Cannot start server: {e2}")

