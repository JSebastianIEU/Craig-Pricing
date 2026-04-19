"""
JustPrint Pricing Data Loader
Loads pricing from JSON files in /data directory.

Source spreadsheets: just_print_pricing.xlsx + just_print_booklet_prices.xlsx
Last updated: April 10, 2026

DO NOT MODIFY prices in the JSON files without Justin's approval.
All prices in EUR excluding VAT.
"""

import json
import os

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def _load(filename: str) -> dict:
    with open(os.path.join(DATA_DIR, filename), "r") as f:
        return json.load(f)


def _intify_prices(data: dict) -> dict:
    """Convert string keys back to int keys for price lookups."""
    for product_key, product in data.items():
        if "prices" in product:
            product["prices"] = {int(k): v for k, v in product["prices"].items()}
    return data


def _intify_booklets(data: dict) -> dict:
    """Convert string keys (pages, quantities) back to ints for booklet lookups."""
    result = {}
    for fmt in data:  # "a5", "a4"
        result[fmt] = {}
        for binding in data[fmt]:  # "saddle_stitch", "perfect_bound"
            result[fmt][binding] = {}
            for pages_str in data[fmt][binding]:
                pages = int(pages_str)
                result[fmt][binding][pages] = {}
                for cover in data[fmt][binding][pages_str]:
                    result[fmt][binding][pages][cover] = {
                        int(q): p for q, p in data[fmt][binding][pages_str][cover].items()
                    }
    return result


# Load all pricing data at startup
SMALL_FORMAT = _intify_prices(_load("small_format.json"))
LARGE_FORMAT = _load("large_format.json")
BOOKLETS = _intify_booklets(_load("booklets.json"))

# Load rules
_rules = _load("rules.json")
SURCHARGES = _rules["surcharges"]
ARTWORK_RATE_EUR = _rules["artwork_rate_eur"]
VAT_RATE = _rules["vat_rate"]
STANDARD_TURNAROUND = _rules["standard_turnaround"]
POA_ITEMS = _rules["poa_items"]
