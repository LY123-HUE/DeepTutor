"""EduBuddy Desktop — a native shell around the DeepTutor web app.

Layers:
    shell  : pywebview (WebView2) window + splash  -> desktop/main.py
    launcher: lifecycle orchestration             -> desktop/launcher.py
    process : deeptutor subprocess + health check -> desktop/process.py
    runtime : Python/Node/deeptutor resolution     -> desktop/runtime.py
"""

__version__ = "0.1.3"
APP_NAME = "EduBuddy"
