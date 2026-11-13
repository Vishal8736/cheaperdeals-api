# Connector files

Drop a `.py` file in this folder and it appears under **Connect > Add a connection** (click *Reload connector files*
if the app is already running). Use it when a company gives you an SDK, or its API does not fit the no-code
"Any company (REST API + key)" form.

1. Download a template from the Connect page (or copy from `examples/`).
2. Rename it, change `key` and `label`, list the settings you need in `fields`.
3. Fill in the methods. If the company has a Python SDK, `pip install` it into the same environment
   (Windows local run: `%USERPROFILE%\CheapeDeal-data\venv\Scripts\pip install <sdk>`), then call it inside the methods.

API keys typed in the form are encrypted in the database and passed to your class as `self.secrets["api_key"]`.
Files starting with `_` and everything in `examples/` are ignored.
