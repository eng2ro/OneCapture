"""Identity & authentication for the multi-tenant spine.

Firm users authenticate through an :class:`AuthProvider` (``DevAuthProvider``
now, ``EntraOIDCProvider`` seam later) and carry a :class:`Principal` resolved
per request. Submitters (claimants) never authenticate — they resolve by
channel binding via :func:`resolve_claimant`.
"""
