"""Unit tests for the merchant-name -> category-slug resolver (no DB)."""

import pytest

from eclaim.services.merchant import merchant_slug


@pytest.mark.parametrize(
    "vendor,slug",
    [
        ("McDonald's Bourke & Russell St", "meals"),
        ("KFC Bukit Bintang", "meals"),
        ("Starbucks KLCC", "meals"),
        ("ZUS Coffee", "meals"),
        ("GrabCar trip", "taxi"),
        ("Uber BV", "taxi"),
        ("Shell Select", "fuel_petrol"),
        ("PETRONAS - EMSI FALAH ENTERPRISE", "fuel_petrol"),
        ("AirAsia Berhad", "air_travel"),
        ("Malaysia Airlines", "air_travel"),
        ("Agoda.com", "hotel"),
        ("Hilton Kuala Lumpur", "hotel"),
        ("KTMB ETS", "rail"),
        ("RapidKL", "public_transport"),
        ("DHL Express", "courier"),
        ("Maxis Broadband", "telco"),
        ("Adobe Systems", "software"),
        ("Wilson Parking", "parking"),
        ("PLUS Highway toll", "tolls"),
        ("Guardian Pharmacy", "medical"),
    ],
)
def test_known_merchants_map_to_slug(vendor, slug):
    assert merchant_slug(vendor) == slug


@pytest.mark.parametrize("vendor", [None, "", "Some Random Sdn Bhd", "Unknown Vendor 123"])
def test_unknown_or_empty_returns_none(vendor):
    assert merchant_slug(vendor) is None


def test_case_insensitive():
    assert merchant_slug("SHELL SELECT") == "fuel_petrol"
    assert merchant_slug("mcdonald's") == "meals"
