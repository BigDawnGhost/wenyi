"""Layout-preserving PDF output through the external BabelDOC HTTP bridge."""

from .export_view import AssembleStore


def _assemble_pdf(
    store: AssembleStore,
    out_path: str,
    *,
    timeout: float,
) -> str:
    """Fill back translations without importing BabelDOC into the MIT runtime."""
    from ..ingest.pdf_babeldoc import translations_from_store
    from ..pdf_bridge import BabeldocBridgeClient

    session_id, bridge_url, mapping = translations_from_store(store)
    client = BabeldocBridgeClient(bridge_url, timeout=timeout)
    return client.fillback(session_id, mapping, out_path=out_path)
