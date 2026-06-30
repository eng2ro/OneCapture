"""Merchant -> expense-category auto-mapping (zero extra staff keying).

A receipt carries a merchant/vendor name; we map it to a category *slug* (the
category's ``expense_type`` key) by keyword — the same idea card platforms use with
ISO 18245 Merchant Category Codes (MCC), but name-based because we read receipts,
not card transactions. The resolved slug is matched against the client's own
category master, so a "McDonald's" receipt lands on **Meals** without the staffer
choosing anything.

Heuristic + Malaysia-aware: brand keywords (Grab, Shell, AirAsia, Agoda, …) plus
generic fallbacks (restaurant, hotel, parking). First matching rule wins, so rules
are ordered specific -> generic. A miss returns ``None`` and the caller falls back
to the OCR ``expense_type`` — so the worst case is "needs review", never a wrong
auto-assign. (Research note: fuel stations sit in the *retail* MCC range, not
transport, which is exactly why production systems pair MCC with merchant-name
rules — this module is the merchant-name half.)
"""

from __future__ import annotations

# (keywords, slug). ``slug`` MUST equal a category.expense_type in the seed
# taxonomy (scripts/seed.py). Order matters — earlier rules win, so brands come
# before broad words, and Meals/F&B sits last so branded venues above it win.
MERCHANT_RULES: list[tuple[tuple[str, ...], str]] = [
    # Fuel stations -> Scope 1 fleet (name-matched: retail MCC, not transport MCC).
    (("shell", "petronas", "petron", "caltex", "bhpetrol", "bhp ", "esso", "mobil"),
     "fuel_petrol"),
    # Air travel -> Scope 3 Cat 6.
    (("airasia", "air asia", "malaysia airlines", "firefly", "batik air", "malindo",
      "singapore airlines", "cathay", "emirates", "qatar airways", "lufthansa",
      "jetstar", "scoot", "airlines", "airways", "airport tax"), "air_travel"),
    # Rail / intercity train.
    (("ktmb", "ktm ", "keretapi", " ets ", "railway", "rail "), "rail"),
    # Urban public transport.
    (("rapidkl", "rapid kl", "myrapid", " mrt", " lrt", "monorail", "rapid penang",
      "rapid bus"), "public_transport"),
    # Taxi / e-hailing.
    (("grab", "uber", "myteksi", "airasia ride", "indriver", "indrive", "bolt",
      "teksi", "e-hailing", "ehailing", "taxi"), "taxi"),
    # Car rental.
    (("hertz", "avis", "europcar", "sixt", "enterprise rent", "budget rent",
      "car rental", "kasina", "mayflower car"), "car_rental"),
    # Hotels / lodging.
    (("hotel", "resort", "agoda", "booking.com", "expedia", "hilton", "marriott",
      "hyatt", "ibis", "holiday inn", "shangri", "traders", "lodge", "inn ",
      "airbnb"), "hotel"),
    # Courier / postage.
    (("pos malaysia", "poslaju", "pos laju", "dhl", "fedex", "ninja van", "ninjavan",
      "j&t", "jnt express", "gdex", "city-link", "citylink", "lalamove", "courier"),
     "courier"),
    # Telco / internet.
    (("maxis", "celcom", "digi", "u mobile", "umobile", "unifi", "telekom",
      "time dotcom", "yes 4g", "yes5g", "redone"), "telco"),
    # Software / SaaS / subscriptions.
    (("microsoft", "google", "aws", "amazon web", "adobe", "zoom", "slack", "github",
      "openai", "anthropic", "dropbox", "atlassian", "notion", "canva", "figma",
      "subscription"), "software"),
    # Office supplies / stationery.
    (("popular book", "mph", "stationery", "stationer", "office supply", "staples",
      "kedai buku"), "office"),
    # Tolls.
    (("touch n go", "touch 'n go", "plus highway", "smarttag", "smart tag", "toll",
      "lekas", "litrak"), "tolls"),
    # Parking.
    (("parking", "car park", "carpark", "wilson park"), "parking"),
    # Medical / pharmacy.
    (("klinik", "clinic", "hospital", "pharmacy", "farmasi", "guardian", "watson"),
     "medical"),
    # Meals & F&B (broad — kept last so branded venues above win).
    (("mcdonald", "kfc", "starbucks", "burger", "pizza", "nando", "subway",
      "restoran", "restaurant", "cafe", "café", "coffee", "kopitiam", "secret recipe",
      "oldtown", "old town", "mamak", "food court", "bakery", "tealive",
      "zus coffee"), "meals"),
]


def merchant_slug(vendor: str | None) -> str | None:
    """Map a merchant/vendor name to a category slug, or ``None`` if unknown.

    Case-insensitive substring match; first matching rule wins. The vendor is
    padded with spaces so rules ending in a space (e.g. ``'ktm '``, ``'inn '``)
    match a trailing token without firing on a longer word."""
    if not vendor:
        return None
    v = " " + vendor.lower() + " "
    for keywords, slug in MERCHANT_RULES:
        if any(kw in v for kw in keywords):
            return slug
    return None
