"""Standardized product-test scenario suite (v1) for the Product Test Report.

Data-only module — edit scenarios here without touching the runner
(`scripts/generate_product_test_report.py`).

Shape:
    GROUPS = [
        {
            "key": "business_cards",          # stable slug (used by --groups filter)
            "title": "Business Cards",        # section title in the report
            "products": ["business_cards"],   # catalog keys covered (for the header)
            "scenarios": [
                {
                    "name": "Direct, full specs",
                    "style": "direct",        # direct | vague | messy | e2e | edge | escalation
                    "turns": ["...customer message per turn..."],
                    # optional assertions → ✓ PASS / ⚠ CHECK in the report:
                    "expect": {
                        "price_contains": "190",     # substring of a € figure in any reply
                        "reply_contains": "...",     # substring anywhere in Craig's replies
                        "reply_not_contains": "...", # must NOT appear (e.g. "ply")
                        "marker": "[QUOTE_READY]",   # marker present in some reply
                        "quote_created": True,       # at least one Quote row produced
                        "escalates": True,           # NO quote row expected (escalation path)
                    },
                },
            ],
        },
    ]

Conventions for end-to-end ("e2e") scenarios:
  - Artwork answered as chat text ("I have my own artwork ready").
  - Contact details given AS CHAT TEXT ONLY ("Test Customer",
    craig.tests@strategos.ai) — the v33 approval email fires only on the
    WIDGET FORM submit, never on in-chat contact capture, so the suite
    sends zero notification emails to the operator.
"""

E2E_CONTACT = "Name is Test Customer, email craig.tests@strategos.ai"

GROUPS = [
    # ── Small format ────────────────────────────────────────────────
    {
        "key": "business_cards",
        "title": "Business Cards",
        "products": ["business_cards"],
        "scenarios": [
            {
                "name": "Direct — full specs in one message",
                "style": "direct",
                "turns": ["Hi, I need 500 business cards, double sided, soft touch lamination"],
                # 500 = €190 base + €15 soft-touch additive (per-product config)
                "expect": {"price_contains": "205", "quote_created": True},
            },
            {
                "name": "Vague opener → full end-to-end funnel",
                "style": "e2e",
                "turns": [
                    "do ye do business cards?",
                    "500 of them",
                    "soft touch, double sided",
                    "I'll send the artwork on later",
                    E2E_CONTACT,
                    "Collection please, and it's for a company — Strategos Test Ltd",
                ],
                "expect": {"marker": "[QUOTE_READY]", "quote_created": True},
            },
            {
                "name": "Typos + slang",
                "style": "messy",
                "turns": ["how much for 250 bizz cards dubble sided?", "gloss is grand"],
                "expect": {"quote_created": True},
            },
        ],
    },
    {
        "key": "flyers",
        "title": "Flyers (A5 / A6 / DL / A4)",
        "products": ["flyers_a5", "flyers_a6", "flyers_dl", "flyers_a4"],
        "scenarios": [
            {
                "name": "A5 double-sided — direct",
                "style": "direct",
                "turns": ["Price for 500 A5 flyers, printed both sides please"],
                "expect": {"price_contains": "132", "quote_created": True},
            },
            {
                "name": "A6 — casual, single sided",
                "style": "vague",
                "turns": ["hiya, looking for some small flyers for handing out", "A6 size", "1000, one side only"],
                "expect": {"quote_created": True},
            },
            {
                "name": "DL — for an envelope mailer",
                "style": "vague",
                "turns": ["I need flyers that fit a standard DL envelope, 500 of them, single sided"],
                "expect": {"quote_created": True},
            },
            {
                "name": "A4 — off-tier quantity (tier stacking)",
                "style": "edge",
                "turns": ["750 A4 flyers single sided — what would that run me?"],
                "expect": {"quote_created": True},
            },
        ],
    },
    {
        "key": "brochures",
        "title": "Brochures — A4 (folds to A5/DL)",
        "products": ["brochures_a4"],
        "scenarios": [
            {
                "name": "Tri-fold — direct (brochures come in gloss/matte)",
                "style": "direct",
                "turns": ["500 A4 brochures folded to DL, tri-fold, double sided",
                          "gloss please"],
                "expect": {"quote_created": True},
            },
            {
                "name": "Z-fold → POA escalation",
                "style": "escalation",
                "turns": ["Can you do 500 z-fold brochures?"],
                "expect": {"escalates": True},
            },
        ],
    },
    {
        "key": "letterheads_compslips",
        "title": "Letterheads & Compliment Slips",
        "products": ["letterheads", "compliment_slips"],
        "scenarios": [
            {
                "name": "Letterheads — direct",
                "style": "direct",
                "turns": ["250 letterheads for our office please", "single sided",
                          "I'll send the artwork on later"],
                "expect": {"quote_created": True},
            },
            {
                "name": "Comp slips — vague + qty later",
                "style": "vague",
                "turns": ["do you print compliment slips?", "500", "DL size, single sided",
                          "I'll send the artwork on later"],
                "expect": {"quote_created": True},
            },
        ],
    },
    {
        "key": "ncr",
        "title": "NCR Books (A5 / A4)",
        "products": ["ncr_books_a5", "ncr_books_a4"],
        "scenarios": [
            {
                "name": "A5 — terminology check (duplicate 2pt, never 'ply')",
                "style": "direct",
                "turns": ["I need NCR books", "A5", "10 books", "duplicate"],
                "expect": {
                    "price_contains": "180",
                    "reply_not_contains": "ply",
                    "quote_created": True,
                },
            },
            {
                "name": "A4 triplicate — +10% surcharge",
                "style": "direct",
                "turns": ["20 A4 NCR books, triplicate (3pt) please"],
                "expect": {"price_contains": "374", "quote_created": True},
            },
            {
                "name": "Carbonless synonym + e2e",
                "style": "e2e",
                "turns": [
                    "do you do those carbonless invoice books?",
                    "A5, 5 books, 2pt",
                    "I'll send the artwork on later",
                    E2E_CONTACT,
                    "Collection please, and it's for a company — Strategos Test Ltd",
                ],
                "expect": {"quote_created": True},
            },
        ],
    },
    {
        "key": "booklets",
        "title": "Booklets (A5 / A4 — saddle stitch & perfect bound)",
        "products": [
            "booklet_a5_saddle_stitch", "booklet_a5_perfect_bound",
            "booklet_a4_saddle_stitch", "booklet_a4_perfect_bound",
        ],
        "scenarios": [
            {
                "name": "A5 saddle stitch, 16pp self cover — direct",
                "style": "direct",
                "turns": ["50 A5 booklets, saddle stitched, 16 pages, self cover"],
                "expect": {"price_contains": "110", "quote_created": True},
            },
            {
                "name": "A5 perfect bound — vague specs gathered over turns",
                "style": "vague",
                "turns": [
                    "I'm putting together a small book, maybe 60 pages",
                    "A5, perfect bound",
                    "100 copies, card cover",
                    "yes that's right — A5",
                ],
                "expect": {"quote_created": True},
            },
            {
                "name": "A4 saddle — card cover defaults UNLAMINATED",
                "style": "edge",
                "turns": ["25 A4 saddle stitch booklets, 32 pages with a card cover",
                          "I'll send the artwork later — what's the price?"],
                "expect": {"quote_created": True},
            },
            {
                "name": "Off-tier qty 80 — documents current stacking behaviour",
                "style": "edge",
                "turns": ["80 A5 saddle stitch booklets, 16 pages, self cover — best price?"],
                "expect": {"quote_created": True},
            },
        ],
    },
    # ── Boards ──────────────────────────────────────────────────────
    {
        "key": "boards",
        "title": "Printed Boards (Corri / Foamex / Dibond)",
        "products": ["corri_boards", "foamex_boards", "dibond_boards"],
        "scenarios": [
            {
                "name": "Corri A3 ×5 — size table",
                "style": "direct",
                "turns": ["5 corri boards, A3 please"],
                "expect": {"price_contains": "70", "quote_created": True},
            },
            {
                "name": "Foamex A1 single — min order bump",
                "style": "edge",
                "turns": ["just one A1 foamex board"],
                "expect": {"quote_created": True},
            },
            {
                "name": "Dibond custom size — laydown calculator",
                "style": "edge",
                "turns": ["I need 5 dibond panels cut at 800 x 600 mm"],
                "expect": {"quote_created": True},
            },
            {
                "name": "Corri full sheet — e2e with artwork upload promise",
                "style": "e2e",
                "turns": [
                    "price for 2 full sheet corri boards, 2440x1220",
                    "I have print-ready artwork",
                    E2E_CONTACT,
                    "Collection please, and it's for a company — Strategos Test Ltd",
                ],
                "expect": {"quote_created": True},
            },
            {
                "name": "Oversize quantity → escalation",
                "style": "escalation",
                "turns": ["I want 5000 A0 corri boards"],
                "expect": {"escalates": True},
            },
        ],
    },
    # ── Posters ─────────────────────────────────────────────────────
    {
        "key": "posters",
        "title": "Posters (190gsm Photo / 220gsm Matt Lam / 220gsm Gloss Lam)",
        "products": ["posters", "posters_220gsm_matt", "posters_220gsm_gloss"],
        "scenarios": [
            {
                "name": "190gsm A1 ×10 — milestone price",
                "style": "direct",
                "turns": ["How much for 10 A1 posters on the standard 190gsm photo paper?"],
                "expect": {"price_contains": "140", "quote_created": True},
            },
            {
                "name": "190gsm A1 ×7 — interpolated quantity",
                "style": "direct",
                "turns": ["I need 7 A1 posters, standard paper"],
                "expect": {"price_contains": "102.20", "quote_created": True},
            },
            {
                "name": "220gsm matt A0 ×25",
                "style": "direct",
                "turns": ["25 A0 posters on the 220gsm with matt lamination"],
                "expect": {"price_contains": "840", "quote_created": True},
            },
            {
                "name": "220gsm gloss — vague, paper chosen mid-chat",
                "style": "vague",
                "turns": ["need some posters for an event", "A2, 50 of them",
                          "I'll send artwork later", "the glossy laminated ones"],
                "expect": {"price_contains": "450", "quote_created": True},
            },
            {
                "name": "Black & white = same price as colour",
                "style": "edge",
                "turns": ["Do you print B&W posters? 10 A1 black and white, standard paper"],
                "expect": {"price_contains": "140", "quote_created": True},
            },
        ],
    },
    # ── Per-square-metre products ───────────────────────────────────
    {
        "key": "vinyl_labels",
        "title": "Vinyl Labels",
        "products": ["vinyl_labels"],
        "scenarios": [
            {
                "name": "Small labels — minimum billable area",
                "style": "edge",
                "turns": ["500 vinyl labels at 40mm x 10mm"],
                "expect": {"quote_created": True},
            },
            {
                "name": "No dimensions given — Craig must ask, not guess",
                "style": "vague",
                "turns": ["how much are vinyl labels?", "200 of them"],
                "expect": {"reply_contains": "size"},
            },
        ],
    },
    {
        "key": "banners_graphics",
        "title": "Banners & Graphics (PVC / Mesh / Window / Floor / Fabric)",
        "products": [
            "pvc_banners", "mesh_banners", "window_graphics",
            "floor_graphics", "fabric_displays",
        ],
        "scenarios": [
            {
                "name": "PVC banner 3m × 1m — direct",
                "style": "direct",
                "turns": ["Price a PVC banner, 3000mm wide by 1000mm high, just the one"],
                "expect": {"quote_created": True},
            },
            {
                "name": "Mesh banner for scaffolding — bulk area",
                "style": "direct",
                "turns": ["mesh banner for scaffolding, 6m x 2m"],
                "expect": {"quote_created": True},
            },
            {
                "name": "Window graphics — vague",
                "style": "vague",
                "turns": ["we want our shop window branded", "just the one window",
                          "about 2m by 1.5m, solid vinyl"],
                "expect": {"quote_created": True},
            },
            {
                "name": "Floor graphics — anti-slip mention",
                "style": "direct",
                "turns": ["4 floor graphics, 600x600mm each, for a retail unit"],
                "expect": {"quote_created": True},
            },
        ],
    },
    {
        "key": "display_unit_products",
        "title": "Roller Banners, Canvas Prints & Vehicle Magnetics",
        "products": ["roller_banners", "canvas_prints", "vehicle_magnetics"],
        "scenarios": [
            {
                "name": "Roller banner ×1 — unit price",
                "style": "direct",
                "turns": ["one roller banner please, the pull-up kind", "just the standard size"],
                "expect": {"price_contains": "120", "quote_created": True},
            },
            {
                "name": "Roller banners ×5 — bulk threshold",
                "style": "direct",
                "turns": ["actually we need 5 pull-up banners for a trade show"],
                "expect": {"price_contains": "550", "quote_created": True},
            },
            {
                "name": "Canvas prints — casual",
                "style": "vague",
                "turns": ["do ye do canvas prints?", "3 of them for the office, 60 x 40 cm"],
                "expect": {"quote_created": True},
            },
            {
                "name": "Vehicle magnetics pair — e2e",
                "style": "e2e",
                "turns": [
                    "2 magnetic signs for a van",
                    "600 x 300 mm each",
                    "I need your design service for the artwork",
                    E2E_CONTACT,
                    "Collection please, and it's for a company — Strategos Test Ltd",
                ],
                "expect": {"quote_created": True},
            },
        ],
    },
    # ── Cross-cutting behaviours ────────────────────────────────────
    {
        "key": "cross_cutting",
        "title": "Cross-cutting Behaviours",
        "products": [],
        "scenarios": [
            {
                "name": "Language mirroring — Spanish enquiry",
                "style": "edge",
                "turns": ["Hola, ¿me pueden cotizar 500 tarjetas de visita a doble cara?", "acabado mate, por favor"],
                "expect": {"quote_created": True},
            },
            {
                "name": "Invalid quantity — negative number rejected",
                "style": "escalation",
                "turns": ["I want -5 business cards"],
                "expect": {"escalates": True},
            },
            {
                "name": "Unknown product → escalation, never invented price",
                "style": "escalation",
                "turns": ["Can you print 200 branded coffee mugs?"],
                "expect": {"escalates": True},
            },
            {
                "name": "A3 poster (not on the list) → escalation",
                "style": "escalation",
                "turns": ["I'd like 20 A3 posters on photo paper"],
                "expect": {"escalates": True},
            },
        ],
    },
]


STYLE_LABELS = {
    "direct": "Direct ask",
    "vague": "Vague / multi-turn",
    "messy": "Typos & slang",
    "e2e": "End-to-end funnel",
    "edge": "Edge case",
    "escalation": "Escalation path",
}
