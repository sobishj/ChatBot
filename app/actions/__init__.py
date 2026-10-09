"""API actions: let the assistant call a client's own API (e.g. appointment booking).

* :mod:`.schema` turns action definitions into model tool definitions.
* :mod:`.executor` validates parameters, guards against SSRF and calls the API.
* :mod:`.pending` keeps per-conversation state: personal-data placeholders and confirmations.
* :mod:`.flow` is the chat path used when a client has actions enabled.
"""
