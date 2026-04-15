from __future__ import annotations

import json
from html import escape
from urllib.parse import quote


def render_viewer_page(
    *,
    viewer_token: str,
    controller: str,
    status_text: str,
    interactive: bool,
) -> str:
    token_path = quote(viewer_token, safe="")
    websocket_path = quote(f"ws/{viewer_token}", safe="/")
    spectator_src = (
        f"/novnc/vnc_lite.html?path={websocket_path}&autoconnect=1&view_only=1&resize=scale"
    )
    interactive_src = f"/novnc/vnc_lite.html?path={websocket_path}&autoconnect=1&resize=scale"
    takeover_path = f"/view/{token_path}/takeover"
    resume_path = f"/view/{token_path}/resume"
    takeover_disabled = " disabled" if controller != "agent" else ""
    resume_disabled = " disabled" if controller != "human" else ""
    initial_src = interactive_src if interactive else spectator_src
    viewer_state_json = json.dumps(
        {
            "viewerToken": viewer_token,
            "controller": controller,
            "interactive": interactive,
            "statusText": status_text,
            "spectatorPath": spectator_src,
            "interactivePath": interactive_src,
            "takeoverPath": takeover_path,
            "resumePath": resume_path,
        }
    ).replace("</", "<\\/")

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
        <div class="status" id="viewer-status">{escape(status_text)}</div>
        <button id="takeover-button" type="button"{takeover_disabled}>Take Over</button>
        <button id="resume-button" type="button"{resume_disabled}>Resume Agent</button>
      </div>
      <iframe
        id="viewer-frame"
        title="Browser session"
        src="{escape(initial_src, quote=True)}"
        allow="clipboard-read; clipboard-write"
      ></iframe>
    </div>
    <script>
      window.browserViewer = {viewer_state_json};

      const viewerState = window.browserViewer;
      const statusNode = document.getElementById("viewer-status");
      const frameNode = document.getElementById("viewer-frame");
      const takeoverButton = document.getElementById("takeover-button");
      const resumeButton = document.getElementById("resume-button");

      function applyControlState(controller, statusText) {{
        viewerState.controller = controller;
        viewerState.interactive = controller === "human";
        frameNode.src = viewerState.interactive
          ? viewerState.interactivePath
          : viewerState.spectatorPath;
        statusNode.textContent = statusText;
        if (controller === "human") {{
          takeoverButton.disabled = true;
          resumeButton.disabled = false;
          return;
        }}
        takeoverButton.disabled = false;
        resumeButton.disabled = true;
      }}

      function enterTerminalState(statusText) {{
        viewerState.controller = "closed";
        viewerState.interactive = false;
        frameNode.src = "about:blank";
        statusNode.textContent = statusText;
        takeoverButton.disabled = true;
        resumeButton.disabled = true;
      }}

      function isTerminalError(detail) {{
        return detail === "viewer session not found" || detail === "no active browser session for this user";
      }}

      applyControlState(viewerState.controller, viewerState.statusText);

      async function sendControlRequest(url, message) {{
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
            if (isTerminalError(payload.detail)) {{
              enterTerminalState("Viewer session ended. Reload to reconnect.");
              return;
            }}
            statusNode.textContent = payload.detail || "Unable to update control.";
            return;
          }}
          applyControlState(payload.controller || viewerState.controller, message);
        }} catch (error) {{
          statusNode.textContent = "Connection issue. Please try again.";
        }}
      }}

      takeoverButton.addEventListener("click", () => {{
        void sendControlRequest(
          viewerState.takeoverPath,
          "Human takeover active. Agent control is paused."
        );
      }});

      resumeButton.addEventListener("click", () => {{
        void sendControlRequest(
          viewerState.resumePath,
          "Agent control resumed. Viewer is back in spectator mode."
        );
      }});
    </script>
  </body>
</html>
"""
