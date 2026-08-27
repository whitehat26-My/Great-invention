"""The demo restaurant's reference data.

A Kuala Lumpur restaurant, so prices are MYR and the menu is local. The data is
deliberately realistic rather than minimal: the forecasting, costing and
scheduling logic only produces meaningful output against a menu with genuine
recipe depth (including sub-recipes), multi-supplier sourcing and a staff roster
with real availability constraints.
"""

from __future__ import annotations

from restaurant_ai.db.models.enums import AccountType, ShiftRole, Station

# --- Allergens --------------------------------------------------------------
ALLERGENS: list[tuple[str, str]] = [
    ("gluten", "Cereals containing gluten"),
    ("peanut", "Peanuts"),
    ("treenut", "Tree nuts"),
    ("shellfish", "Crustaceans and molluscs"),
    ("fish", "Fish"),
    ("egg", "Egg"),
    ("dairy", "Milk and dairy"),
    ("soy", "Soybeans"),
    ("sesame", "Sesame"),
]

# --- Ingredients ------------------------------------------------------------
# (code, name, base_uom, cost_per_base_unit, yield_pct, shelf_life_days, allergens)
INGREDIENTS: list[tuple[str, str, str, str, str, int, str]] = [
    # Proteins
    ("ING-CHKN-THI", "Chicken thigh, boneless", "g", "0.0185", "0.92", 3, ""),
    ("ING-BEEF-CHK", "Beef chuck, diced", "g", "0.0420", "0.88", 3, ""),
    ("ING-PRWN-MED", "Prawns, medium, peeled", "g", "0.0560", "0.90", 2, "shellfish"),
    ("ING-ANCH-DRY", "Anchovies, dried", "g", "0.0640", "1.00", 90, "fish"),
    ("ING-FISH-CKE", "Fish cake, sliced", "g", "0.0310", "1.00", 5, "fish,gluten"),
    ("ING-EGG-WHL", "Eggs, whole", "ea", "0.5200", "1.00", 21, "egg"),
    ("ING-TOFU-FRM", "Tofu, firm", "g", "0.0090", "0.95", 5, "soy"),
    # Produce
    ("ING-ONI-RED", "Onion, red", "g", "0.0058", "0.85", 21, ""),
    ("ING-GAR-PEE", "Garlic, peeled", "g", "0.0150", "0.95", 30, ""),
    ("ING-GIN-FRH", "Ginger, fresh", "g", "0.0120", "0.82", 21, ""),
    ("ING-LEM-GRS", "Lemongrass", "g", "0.0110", "0.60", 14, ""),
    ("ING-GAL-FRH", "Galangal", "g", "0.0135", "0.75", 14, ""),
    ("ING-TUR-FRH", "Turmeric, fresh", "g", "0.0180", "0.80", 21, ""),
    ("ING-CHI-DRY", "Chilli, dried", "g", "0.0290", "1.00", 90, ""),
    ("ING-CHI-BRD", "Chilli, bird's eye", "g", "0.0340", "0.95", 10, ""),
    ("ING-CUC-JAP", "Cucumber", "g", "0.0048", "0.88", 10, ""),
    ("ING-BSP-MUN", "Bean sprouts", "g", "0.0062", "0.90", 3, ""),
    ("ING-BAN-PIS", "Banana, pisang raja", "ea", "0.7500", "0.70", 6, ""),
    ("ING-PAN-LEF", "Pandan leaf", "g", "0.0200", "1.00", 10, ""),
    ("ING-LIM-KAL", "Kaffir lime leaf", "g", "0.0480", "1.00", 14, ""),
    # Dry / pantry
    ("ING-RICE-JAS", "Jasmine rice", "g", "0.0052", "1.00", 365, ""),
    ("ING-NOOD-KWY", "Kway teow, flat rice", "g", "0.0082", "1.00", 4, ""),
    ("ING-NOOD-VER", "Rice vermicelli", "g", "0.0075", "1.00", 180, ""),
    ("ING-FLOU-APP", "Flour, all purpose", "g", "0.0038", "1.00", 180, "gluten"),
    ("ING-COCO-MLK", "Coconut milk", "ml", "0.0094", "1.00", 14, ""),
    ("ING-OIL-PLM", "Cooking oil, palm", "ml", "0.0068", "1.00", 365, ""),
    ("ING-SUG-PLM", "Palm sugar (gula melaka)", "g", "0.0155", "1.00", 365, ""),
    ("ING-SUG-WHT", "Sugar, white", "g", "0.0034", "1.00", 365, ""),
    ("ING-SALT-SEA", "Salt", "g", "0.0012", "1.00", 999, ""),
    ("ING-SOY-DRK", "Soy sauce, dark", "ml", "0.0110", "1.00", 365, "soy,gluten"),
    ("ING-TAM-PLP", "Tamarind pulp", "g", "0.0210", "1.00", 180, ""),
    ("ING-BLC-SHR", "Belacan (shrimp paste)", "g", "0.0380", "1.00", 365, "shellfish,fish"),
    ("ING-COC-DES", "Coconut, desiccated", "g", "0.0165", "1.00", 180, ""),
    ("ING-PNT-ROA", "Peanuts, roasted", "g", "0.0245", "1.00", 90, "peanut"),
    ("ING-CEN-JEL", "Cendol jelly", "g", "0.0130", "1.00", 5, ""),
    # Beverage
    ("ING-TEA-DUS", "Tea dust, local blend", "g", "0.0290", "1.00", 365, ""),
    ("ING-COF-POW", "Coffee powder, kopi", "g", "0.0410", "1.00", 180, ""),
    ("ING-MLK-CON", "Condensed milk", "ml", "0.0125", "1.00", 365, "dairy"),
    ("ING-MLK-EVP", "Evaporated milk", "ml", "0.0098", "1.00", 365, "dairy"),
    ("ING-ICE-VAN", "Ice cream, vanilla", "ml", "0.0180", "1.00", 180, "dairy,egg"),
    ("ING-LEM-JUI", "Lemon juice", "ml", "0.0155", "1.00", 14, ""),
]

# --- Sub-recipes ------------------------------------------------------------
# These are batch-produced and consumed by plated recipes, which is what makes
# the BOM explosion genuinely recursive.
# (code, name, yield_qty, yield_uom, station, prep_seconds, components)
SUB_RECIPES: list[tuple[str, str, str, str, Station, int, list[tuple[str, str, str]]]] = [
    (
        "SUB-REND-PST",
        "Rendang spice paste",
        "1200",
        "g",
        Station.SAUTE,
        2700,
        [
            ("ING-CHI-DRY", "120", "g"),
            ("ING-ONI-RED", "400", "g"),
            ("ING-GAR-PEE", "90", "g"),
            ("ING-GIN-FRH", "80", "g"),
            ("ING-GAL-FRH", "90", "g"),
            ("ING-LEM-GRS", "120", "g"),
            ("ING-TUR-FRH", "60", "g"),
            ("ING-OIL-PLM", "240", "ml"),
        ],
    ),
    (
        "SUB-SAMB-TUM",
        "Sambal tumis",
        "1000",
        "g",
        Station.SAUTE,
        2400,
        [
            ("ING-CHI-DRY", "150", "g"),
            ("ING-ONI-RED", "350", "g"),
            ("ING-GAR-PEE", "60", "g"),
            ("ING-BLC-SHR", "40", "g"),
            ("ING-TAM-PLP", "70", "g"),
            ("ING-SUG-PLM", "90", "g"),
            ("ING-OIL-PLM", "260", "ml"),
        ],
    ),
    (
        "SUB-CURR-BSE",
        "Curry base",
        "1500",
        "ml",
        Station.SAUTE,
        1800,
        [
            ("ING-ONI-RED", "300", "g"),
            ("ING-GAR-PEE", "70", "g"),
            ("ING-GIN-FRH", "70", "g"),
            ("ING-CHI-DRY", "90", "g"),
            ("ING-TUR-FRH", "50", "g"),
            ("ING-COCO-MLK", "800", "ml"),
            ("ING-OIL-PLM", "150", "ml"),
        ],
    ),
    (
        "SUB-COCO-RCE",
        "Coconut rice (nasi lemak base)",
        "3000",
        "g",
        Station.SAUTE,
        3000,
        [
            ("ING-RICE-JAS", "1500", "g"),
            ("ING-COCO-MLK", "900", "ml"),
            ("ING-PAN-LEF", "25", "g"),
            ("ING-GIN-FRH", "30", "g"),
            ("ING-SALT-SEA", "22", "g"),
        ],
    ),
]

# --- Menu -------------------------------------------------------------------
# (section, display_order)
MENU_SECTIONS: list[tuple[str, int]] = [
    ("Starters", 1),
    ("Rice & Noodles", 2),
    ("Grill", 3),
    ("Desserts", 4),
    ("Drinks", 5),
]

# (sku, name, section, price, station, prep_seconds, course, description, components)
MENU_ITEMS: list[tuple[str, str, str, str, Station, int, int, str, list[tuple[str, str, str]]]] = [
    (
        "MNU-ROTIJALA",
        "Roti Jala with Chicken Curry",
        "Starters",
        "14.90",
        Station.SAUTE,
        420,
        1,
        "Lacy net pancakes with a rich chicken curry dip.",
        [
            ("ING-FLOU-APP", "90", "g"),
            ("ING-EGG-WHL", "1", "ea"),
            ("ING-COCO-MLK", "70", "ml"),
            ("SUB-CURR-BSE", "150", "ml"),
            ("ING-CHKN-THI", "80", "g"),
        ],
    ),
    (
        "MNU-POPIAHGR",
        "Popiah Goreng",
        "Starters",
        "12.50",
        Station.FRY,
        300,
        1,
        "Crisp spring rolls with turnip, tofu and prawn.",
        [
            ("ING-FLOU-APP", "60", "g"),
            ("ING-TOFU-FRM", "70", "g"),
            ("ING-PRWN-MED", "45", "g"),
            ("ING-BSP-MUN", "50", "g"),
            ("ING-OIL-PLM", "40", "ml"),
        ],
    ),
    (
        "MNU-PRWNFRIT",
        "Sambal Prawn Fritters",
        "Starters",
        "18.90",
        Station.FRY,
        360,
        1,
        "Golden prawn fritters with sambal tumis.",
        [
            ("ING-PRWN-MED", "110", "g"),
            ("ING-FLOU-APP", "70", "g"),
            ("ING-EGG-WHL", "1", "ea"),
            ("SUB-SAMB-TUM", "45", "g"),
            ("ING-OIL-PLM", "60", "ml"),
        ],
    ),
    (
        "MNU-NASILEMK",
        "Nasi Lemak Ayam Rendang",
        "Rice & Noodles",
        "24.90",
        Station.SAUTE,
        540,
        2,
        "Coconut rice, beef-style chicken rendang, sambal, egg, anchovies.",
        [
            ("SUB-COCO-RCE", "320", "g"),
            ("ING-CHKN-THI", "180", "g"),
            ("SUB-REND-PST", "70", "g"),
            ("SUB-SAMB-TUM", "50", "g"),
            ("ING-COCO-MLK", "80", "ml"),
            ("ING-EGG-WHL", "1", "ea"),
            ("ING-ANCH-DRY", "20", "g"),
            ("ING-PNT-ROA", "15", "g"),
            ("ING-CUC-JAP", "40", "g"),
        ],
    ),
    (
        "MNU-BEEFREND",
        "Beef Rendang Rice",
        "Rice & Noodles",
        "28.90",
        Station.SAUTE,
        600,
        2,
        "Slow-cooked beef rendang with coconut rice.",
        [
            ("SUB-COCO-RCE", "320", "g"),
            ("ING-BEEF-CHK", "200", "g"),
            ("SUB-REND-PST", "90", "g"),
            ("ING-COCO-MLK", "120", "ml"),
            ("ING-COC-DES", "25", "g"),
            ("ING-CUC-JAP", "40", "g"),
        ],
    ),
    (
        "MNU-CHARKWAY",
        "Char Kway Teow",
        "Rice & Noodles",
        "19.90",
        Station.SAUTE,
        420,
        2,
        "Wok-fried flat noodles with prawn, egg and bean sprouts.",
        [
            ("ING-NOOD-KWY", "220", "g"),
            ("ING-PRWN-MED", "70", "g"),
            ("ING-EGG-WHL", "1", "ea"),
            ("ING-BSP-MUN", "60", "g"),
            ("ING-FISH-CKE", "40", "g"),
            ("ING-SOY-DRK", "25", "ml"),
            ("ING-OIL-PLM", "35", "ml"),
            ("ING-GAR-PEE", "12", "g"),
        ],
    ),
    (
        "MNU-LAKSALEM",
        "Laksa Lemak",
        "Rice & Noodles",
        "22.90",
        Station.SAUTE,
        480,
        2,
        "Rice vermicelli in a coconut curry broth with prawn and tofu.",
        [
            ("ING-NOOD-VER", "180", "g"),
            ("SUB-CURR-BSE", "300", "ml"),
            ("ING-COCO-MLK", "150", "ml"),
            ("ING-PRWN-MED", "60", "g"),
            ("ING-TOFU-FRM", "60", "g"),
            ("ING-BSP-MUN", "50", "g"),
            ("ING-LIM-KAL", "3", "g"),
        ],
    ),
    (
        "MNU-NASIGORV",
        "Vegetarian Nasi Goreng",
        "Rice & Noodles",
        "17.90",
        Station.SAUTE,
        390,
        2,
        "Fried rice with tofu, vegetables and house chilli.",
        [
            ("ING-RICE-JAS", "260", "g"),
            ("ING-TOFU-FRM", "90", "g"),
            ("ING-BSP-MUN", "50", "g"),
            ("ING-CHI-BRD", "8", "g"),
            ("ING-SOY-DRK", "20", "ml"),
            ("ING-OIL-PLM", "30", "ml"),
            ("ING-GAR-PEE", "12", "g"),
        ],
    ),
    (
        "MNU-GRILCHOP",
        "Grilled Chicken Chop",
        "Grill",
        "26.90",
        Station.GRILL,
        660,
        2,
        "Marinated chicken thigh, grilled, with sambal butter.",
        [
            ("ING-CHKN-THI", "260", "g"),
            ("SUB-SAMB-TUM", "40", "g"),
            ("ING-GAR-PEE", "10", "g"),
            ("ING-OIL-PLM", "20", "ml"),
            ("ING-CUC-JAP", "50", "g"),
        ],
    ),
    (
        "MNU-GRILPRWN",
        "Grilled Prawn Skewers",
        "Grill",
        "32.90",
        Station.GRILL,
        540,
        2,
        "Tiger prawns, lemongrass marinade, charred over flame.",
        [
            ("ING-PRWN-MED", "220", "g"),
            ("ING-LEM-GRS", "20", "g"),
            ("ING-TUR-FRH", "8", "g"),
            ("ING-GAR-PEE", "10", "g"),
            ("ING-OIL-PLM", "25", "ml"),
        ],
    ),
    (
        "MNU-CENDOL",
        "Cendol Gula Melaka",
        "Desserts",
        "10.90",
        Station.PASTRY,
        180,
        3,
        "Shaved ice, coconut milk, palm sugar and pandan jelly.",
        [("ING-CEN-JEL", "80", "g"), ("ING-COCO-MLK", "120", "ml"), ("ING-SUG-PLM", "45", "g")],
    ),
    (
        "MNU-PISANGGR",
        "Pisang Goreng with Ice Cream",
        "Desserts",
        "13.90",
        Station.PASTRY,
        300,
        3,
        "Fried banana fritters with vanilla ice cream.",
        [
            ("ING-BAN-PIS", "2", "ea"),
            ("ING-FLOU-APP", "60", "g"),
            ("ING-OIL-PLM", "50", "ml"),
            ("ING-ICE-VAN", "90", "ml"),
            ("ING-SUG-PLM", "20", "g"),
        ],
    ),
    (
        "MNU-TEHTARIK",
        "Teh Tarik",
        "Drinks",
        "6.50",
        Station.BAR,
        120,
        4,
        "Pulled milk tea, frothy and strong.",
        [("ING-TEA-DUS", "12", "g"), ("ING-MLK-CON", "45", "ml"), ("ING-MLK-EVP", "40", "ml")],
    ),
    (
        "MNU-KOPIO",
        "Kopi O",
        "Drinks",
        "5.50",
        Station.BAR,
        90,
        4,
        "Black local coffee, sweetened.",
        [("ING-COF-POW", "16", "g"), ("ING-SUG-WHT", "12", "g")],
    ),
    (
        "MNU-ICELEMON",
        "Iced Lemon Tea",
        "Drinks",
        "7.50",
        Station.BAR,
        120,
        4,
        "Chilled tea with fresh lemon.",
        [("ING-TEA-DUS", "10", "g"), ("ING-LEM-JUI", "30", "ml"), ("ING-SUG-WHT", "18", "g")],
    ),
]

# --- Suppliers --------------------------------------------------------------
# (code, name, email, lead_time_days, min_order_value, delivery_days)
SUPPLIERS: list[tuple[str, str, str, int, str, str]] = [
    (
        "SUP-FRESH",
        "Pasar Borong Fresh Produce",
        "orders@pasarborong.example",
        1,
        "150.00",
        "0,1,2,3,4,5",
    ),
    ("SUP-MEATS", "Sing Long Meats & Seafood", "sales@singlong.example", 2, "300.00", "0,2,4"),
    ("SUP-DRY", "Hock Seng Dry Goods", "po@hockseng.example", 3, "200.00", "1,4"),
]

# Which supplier carries which ingredient, and in what pack.
# (ingredient_code, supplier_code, supplier_sku, pack_size, pack_uom, contract_price, moq)
STOCK_ITEMS: list[tuple[str, str, str, str, str, str, str]] = [
    ("ING-CHKN-THI", "SUP-MEATS", "SL-CHK-5K", "5000", "kg", "92.50", "1"),
    ("ING-BEEF-CHK", "SUP-MEATS", "SL-BEF-3K", "3000", "kg", "126.00", "1"),
    ("ING-PRWN-MED", "SUP-MEATS", "SL-PRW-2K", "2000", "kg", "112.00", "1"),
    ("ING-FISH-CKE", "SUP-MEATS", "SL-FSH-1K", "1000", "kg", "31.00", "2"),
    ("ING-EGG-WHL", "SUP-FRESH", "PB-EGG-30", "30", "tray", "15.60", "2"),
    ("ING-TOFU-FRM", "SUP-FRESH", "PB-TOF-2K", "2000", "kg", "18.00", "1"),
    ("ING-ONI-RED", "SUP-FRESH", "PB-ONI-10K", "10000", "kg", "58.00", "1"),
    ("ING-GAR-PEE", "SUP-FRESH", "PB-GAR-2K", "2000", "kg", "30.00", "1"),
    ("ING-GIN-FRH", "SUP-FRESH", "PB-GIN-2K", "2000", "kg", "24.00", "1"),
    ("ING-LEM-GRS", "SUP-FRESH", "PB-LEM-1K", "1000", "kg", "11.00", "1"),
    ("ING-GAL-FRH", "SUP-FRESH", "PB-GAL-1K", "1000", "kg", "13.50", "1"),
    ("ING-TUR-FRH", "SUP-FRESH", "PB-TUR-1K", "1000", "kg", "18.00", "1"),
    ("ING-CHI-BRD", "SUP-FRESH", "PB-CHB-1K", "1000", "kg", "34.00", "1"),
    ("ING-CUC-JAP", "SUP-FRESH", "PB-CUC-5K", "5000", "kg", "24.00", "1"),
    ("ING-BSP-MUN", "SUP-FRESH", "PB-BSP-3K", "3000", "kg", "18.60", "1"),
    ("ING-BAN-PIS", "SUP-FRESH", "PB-BAN-20", "20", "comb", "15.00", "1"),
    ("ING-PAN-LEF", "SUP-FRESH", "PB-PAN-500", "500", "bunch", "10.00", "1"),
    ("ING-LIM-KAL", "SUP-FRESH", "PB-LIM-200", "200", "pack", "9.60", "1"),
    ("ING-CEN-JEL", "SUP-FRESH", "PB-CEN-2K", "2000", "kg", "26.00", "1"),
    ("ING-LEM-JUI", "SUP-FRESH", "PB-LMJ-1L", "1000", "btl", "15.50", "2"),
    ("ING-RICE-JAS", "SUP-DRY", "HS-RIC-25K", "25000", "sack", "130.00", "1"),
    ("ING-NOOD-KWY", "SUP-DRY", "HS-KWY-5K", "5000", "pack", "41.00", "1"),
    ("ING-NOOD-VER", "SUP-DRY", "HS-VER-5K", "5000", "pack", "37.50", "1"),
    ("ING-FLOU-APP", "SUP-DRY", "HS-FLR-25K", "25000", "sack", "95.00", "1"),
    ("ING-COCO-MLK", "SUP-DRY", "HS-COC-12L", "12000", "case", "112.80", "1"),
    ("ING-OIL-PLM", "SUP-DRY", "HS-OIL-17L", "17000", "tin", "115.60", "1"),
    ("ING-SUG-PLM", "SUP-DRY", "HS-GML-5K", "5000", "box", "77.50", "1"),
    ("ING-SUG-WHT", "SUP-DRY", "HS-SUG-25K", "25000", "sack", "85.00", "1"),
    ("ING-SALT-SEA", "SUP-DRY", "HS-SLT-10K", "10000", "sack", "12.00", "1"),
    ("ING-SOY-DRK", "SUP-DRY", "HS-SOY-5L", "5000", "jug", "55.00", "1"),
    ("ING-TAM-PLP", "SUP-DRY", "HS-TAM-2K", "2000", "box", "42.00", "1"),
    ("ING-BLC-SHR", "SUP-DRY", "HS-BLC-1K", "1000", "block", "38.00", "1"),
    ("ING-COC-DES", "SUP-DRY", "HS-DES-2K", "2000", "pack", "33.00", "1"),
    ("ING-PNT-ROA", "SUP-DRY", "HS-PNT-3K", "3000", "pack", "73.50", "1"),
    ("ING-ANCH-DRY", "SUP-DRY", "HS-ANC-2K", "2000", "pack", "128.00", "1"),
    ("ING-CHI-DRY", "SUP-DRY", "HS-CHD-3K", "3000", "pack", "87.00", "1"),
    ("ING-TEA-DUS", "SUP-DRY", "HS-TEA-5K", "5000", "pack", "145.00", "1"),
    ("ING-COF-POW", "SUP-DRY", "HS-COF-3K", "3000", "pack", "123.00", "1"),
    ("ING-MLK-CON", "SUP-DRY", "HS-MCD-12L", "12000", "case", "150.00", "1"),
    ("ING-MLK-EVP", "SUP-DRY", "HS-MEV-12L", "12000", "case", "117.60", "1"),
    ("ING-ICE-VAN", "SUP-DRY", "HS-ICE-5L", "5000", "tub", "90.00", "1"),
]

# --- Dining room ------------------------------------------------------------
# (label, seats, section, combinable)
TABLES: list[tuple[str, int, str, bool]] = [
    ("T1", 2, "window", True),
    ("T2", 2, "window", True),
    ("T3", 2, "window", True),
    ("T4", 4, "main", True),
    ("T5", 4, "main", True),
    ("T6", 4, "main", True),
    ("T7", 4, "main", True),
    ("T8", 6, "main", False),
    ("T9", 6, "main", False),
    ("T10", 8, "private", False),
    ("B1", 2, "bar", False),
    ("B2", 2, "bar", False),
]

# --- Staff ------------------------------------------------------------------
# (code, name, role, hourly_rate, max_weekly_hours)
STAFF: list[tuple[str, str, ShiftRole, float, int]] = [
    ("EMP-001", "Aishah Rahman", ShiftRole.MANAGER, 32.00, 45),
    ("EMP-002", "Chandran Nair", ShiftRole.CHEF, 28.50, 45),
    ("EMP-003", "Wong Mei Ling", ShiftRole.CHEF, 27.00, 45),
    ("EMP-004", "Faizal Hamid", ShiftRole.LINE_COOK, 18.50, 44),
    ("EMP-005", "Siti Nurhaliza", ShiftRole.LINE_COOK, 18.00, 44),
    ("EMP-006", "Ravi Kumar", ShiftRole.LINE_COOK, 17.50, 44),
    ("EMP-007", "Lim Jia Hui", ShiftRole.KITCHEN_PORTER, 14.00, 44),
    ("EMP-008", "Nurul Izzah", ShiftRole.SERVER, 15.50, 44),
    ("EMP-009", "Tan Wei Sheng", ShiftRole.SERVER, 15.50, 44),
    ("EMP-010", "Priya Devi", ShiftRole.SERVER, 15.00, 44),
    ("EMP-011", "Amirul Hakim", ShiftRole.SERVER, 15.00, 40),
    ("EMP-012", "Grace Anak Joseph", ShiftRole.HOST, 16.00, 40),
    ("EMP-013", "Danial Aziz", ShiftRole.BARISTA, 16.50, 40),
]

# --- Chart of accounts ------------------------------------------------------
# (code, name, type)
LEDGER_ACCOUNTS: list[tuple[str, str, AccountType]] = [
    ("1000", "Cash on Hand", AccountType.ASSET),
    ("1010", "Bank - Operating", AccountType.ASSET),
    ("1020", "Card Settlements Receivable", AccountType.ASSET),
    ("1030", "Delivery Platform Receivable", AccountType.ASSET),
    ("1200", "Inventory - Food", AccountType.ASSET),
    ("2000", "Accounts Payable", AccountType.LIABILITY),
    ("2100", "Sales Tax Payable", AccountType.LIABILITY),
    ("3000", "Owner's Equity", AccountType.EQUITY),
    ("4000", "Food Sales", AccountType.REVENUE),
    ("4010", "Beverage Sales", AccountType.REVENUE),
    ("4900", "Discounts Given", AccountType.REVENUE),
    ("5000", "Cost of Goods Sold", AccountType.EXPENSE),
    ("5100", "Food Waste", AccountType.EXPENSE),
    ("6000", "Labour - Wages", AccountType.EXPENSE),
    ("6100", "Delivery Commission", AccountType.EXPENSE),
    ("6200", "Card Processing Fees", AccountType.EXPENSE),
    ("6900", "Cash Variance", AccountType.EXPENSE),
]

# --- Standard operating procedures ------------------------------------------
# (slug, title, category, applies_to_role, body)
SOPS: list[tuple[str, str, str, str | None, str]] = [
    (
        "opening-checklist",
        "Kitchen Opening Checklist",
        "operations",
        "line_cook",
        "Arrive 90 minutes before service. Switch on the hood extraction before any burner. "
        "Check walk-in chiller temperature is at or below 4C and freezer at or below -18C; "
        "log both on the temperature sheet. Pull the prep list from the KDS terminal and "
        "confirm quantities against yesterday's waste log. Sanitise all cutting boards. "
        "Fire the rice cookers by 10:00 so coconut rice is rested before the lunch push.",
    ),
    (
        "closing-checklist",
        "Kitchen Closing Checklist",
        "operations",
        "line_cook",
        "Break down and sanitise every station. Date-label and blast-chill all remaining "
        "sub-recipes; rendang paste and sambal tumis keep five days refrigerated. Record all "
        "spoilage on the waste log with the reason code. Filter and strain the fryer oil; "
        "replace when the test strip reads above 25 percent polar compounds. Final walk-in "
        "temperature check, then switch off the hood last.",
    ),
    (
        "allergen-handling",
        "Allergen Handling Procedure",
        "food-safety",
        None,
        "Any order flagged with an allergen must be prepared on a sanitised board with clean "
        "utensils and a colour-coded purple allergen pan. Shellfish and peanut are the two "
        "highest-risk allergens on this menu: belacan in sambal tumis contains shrimp, so any "
        "dish using sambal is not shellfish-safe. Nasi lemak is garnished with roasted peanuts "
        "and must be plated without them for peanut allergies. Never rely on memory: check the "
        "ticket, confirm with the expeditor, and hand the plate over verbally.",
    ),
    (
        "food-safety-temps",
        "Temperature and Holding Standards",
        "food-safety",
        None,
        "Cook chicken to a core temperature of 74C and beef rendang to at least 71C before the "
        "long braise. Hot-hold above 63C and cold-hold below 5C. Nothing sits in the danger zone "
        "between 5C and 63C for more than two hours in total. Cool cooked sub-recipes from 60C "
        "to 21C within two hours and to 5C within a further four. Discard anything past its "
        "date label without exception.",
    ),
    (
        "shift-swap-policy",
        "Shift Swap Policy",
        "hr",
        None,
        "Swaps must be requested at least 48 hours before the shift starts. Both staff must hold "
        "the role the shift requires; a server cannot cover a line cook slot. The swap must not "
        "push either person past their contracted weekly hours or break the 11-hour minimum rest "
        "between shifts. Submit the request through the staff assistant, which routes it to the "
        "duty manager for approval. Unapproved swaps do not release you from the original shift.",
    ),
    (
        "cash-handling",
        "Cash Handling and End of Day",
        "finance",
        "manager",
        "Count the float at open and record it. At close, count the drawer twice with a second "
        "person present. Enter the counted figure into the POS cash-up screen; the bookkeeping "
        "agent reconciles it against recorded takings overnight. Any variance above 20 ringgit "
        "must be explained in the notes field the same evening. Bank the takings the next "
        "business morning.",
    ),
    (
        "guest-complaint",
        "Handling a Guest Complaint",
        "service",
        "server",
        "Listen without interrupting and do not argue the facts at the table. Acknowledge, "
        "apologise for the experience, and fix what can be fixed immediately: remake the dish, "
        "remove the item, or offer a dessert. Anything involving illness, an allergic reaction, "
        "or a foreign object goes to the duty manager straight away and is written up the same "
        "shift. Never promise a refund without the manager.",
    ),
    (
        "wok-station",
        "Wok Station Standards",
        "recipes",
        "line_cook",
        "The wok must be smoking before anything goes in; char kway teow depends on wok hei and "
        "will taste flat from a cool pan. Cook one portion at a time. Egg goes in against the "
        "side of the wok, not on top of the noodles. Bean sprouts go in during the last 20 "
        "seconds so they stay crisp. Wipe and re-oil the wok between orders that carry an "
        "allergen flag.",
    ),
    (
        "rendang-method",
        "Beef Rendang Method",
        "recipes",
        "chef",
        "Bloom the rendang paste in oil until the oil separates and the paste darkens; this is "
        "the single biggest driver of the finished flavour and takes longer than most cooks "
        "expect. Add the beef and coat, then coconut milk, and hold at the barest simmer for "
        "three to four hours until the sauce is dry and clinging. Toasted desiccated coconut "
        "(kerisik) goes in during the final 20 minutes. The dish is finished when the oil comes "
        "back out of the sauce.",
    ),
    (
        "waste-logging",
        "Waste Logging",
        "operations",
        None,
        "Every discard is logged before it goes in the bin: ingredient, weight, and reason code "
        "(spoilage, over-prep, cooking error, guest return). The prep forecaster reads this log "
        "to correct the next day's quantities, so an unlogged discard becomes tomorrow's "
        "over-prep. Weigh, do not estimate.",
    ),
]
