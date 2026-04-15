from __future__ import annotations

from html import escape


def render_viewer_page(*, viewer_token: str) -> str:
    token = escape(viewer_token, quote=True)
    novnc_src = f"/novnc/vnc_lite.html?path=ws/{token}&autoconnect=1&resize=scale"

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Browser Viewer</title>
    <style>
      :root {{
        color-scheme: light;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }}
      body {{
        margin: 0;
        background: #f3f4f6;
        color: #111827;
      }}
      .shell {{
        display: flex;
        flex-direction: column;
        min-height: 100vh;
      }}
      .toolbar {{
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 12px 16px;
        background: #ffffff;
        border-bottom: 1px solid #d1d5db;
      }}
      .status {{
        flex: 1;
        font-size: 14px;
      }}
      button {{
        border: 1px solid #9ca3af;
        background: #ffffff;
        color: #111827;
        border-radius: 8px;
        padding: 8px 12px;
        font-size: 14px;
        cursor: pointer;
      }}
      iframe {{
        flex: 1;
        width: 100%;
        min-height: 0;
        border: 0;
        background: #000000;
      }}
    </style>
  </head>
  <body>
    <div class="shell">
      <div class="toolbar">
        <div class="status" id="viewer-status">Viewer ready. Use Take Over to pause the agent or Resume Agent to hand control back.</div>
        <button id="takeover-button" type="button">Take Over</button>
        <button id="resume-button" type="button">Resume Agent</button>
      </div>
      <iframe
        title="Browser session"
        src="{novnc_src}"
        allow="clipboard-read; clipboard-write"
      ></iframe>
    </div>
    <script>
      window.browserViewer = {{
        viewerToken: "{token}",
        novncPath: "{novnc_src}"
      }};
    </script>
  </body>
</html>
"""
