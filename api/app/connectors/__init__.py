"""Connectors: how Cheape Deal talks to any supplier company or any website.

Three ways to connect, easiest first:
  1. Built-in types with just an API key (CJ Dropshipping, WooCommerce, ...).
  2. "Generic REST API" / "Custom website": describe any company's HTTP API in the UI (no code).
  3. Connector file: drop a small .py file in the connectors/ folder (or upload it in the UI). Use this when a company
     gives you an SDK, or its API is too special for option 2. See connectors/examples/.
"""
