from __future__ import annotations

from html import escape


def render_viewer_page(*, viewer_token: str) -> str:
    token = escape(viewer_token, quote=True)
    spectator_src = (
        f"/novnc/vnc_lite.html?path=ws/{token}&autoconnect=1&view_only=1&resize=scale"
    )
    interactive_src = f"/novnc/vnc_lite.html?path=ws/{token}&autoconnect=1&resize=scale"
    takeover_path = f"/view/{token}/takeover"
    resume_path = f"/view/{token}/resume"

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
        id="viewer-frame"
        title="Browser session"
        src="{spectator_src}"
        allow="clipboard-read; clipboard-write"
      ></iframe>
    </div>
    <script>
      window.browserViewer = {{
        viewerToken: "{token}",
        spectatorPath: "{spectator_src}",
        interactivePath: "{interactive_src}",
        takeoverPath: "{takeover_path}",
        resumePath: "{resume_path}"
      }};

      const viewerState = window.browserViewer;
      const statusNode = document.getElementById("viewer-status");
      const frameNode = document.getElementById("viewer-frame");
      const takeoverButton = document.getElementById("takeover-button");
      const resumeButton = document.getElementById("resume-button");

      async function sendControlRequest(url, nextSrc, message) {{
        statusNode.textContent = "Updating control...";
        try {{
          const response = await fetch(url, {{ method: "POST" }});
          let payload = {{}};
          try {{
            payload = await response.json();
          }} catch (error) {{
            payload = {{}};
          }}
          if (!response.ok) {{
            statusNode.textContent = payload.detail || "Unable to update control.";
            return;
          }}
          frameNode.src = nextSrc;
          statusNode.textContent = message;
        }} catch (error) {{
          statusNode.textContent = "Connection issue. Please try again.";
        }}
      }}

      takeoverButton.addEventListener("click", () => {{
        void sendControlRequest(
          viewerState.takeoverPath,
          viewerState.interactivePath,
          "Human takeover active. Agent control is paused."
        );
      }});

      resumeButton.addEventListener("click", () => {{
        void sendControlRequest(
          viewerState.resumePath,
          viewerState.spectatorPath,
          "Agent control resumed. Viewer is back in spectator mode."
        );
      }});
    </script>
  </body>
</html>
"""
