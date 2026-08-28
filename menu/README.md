# The restaurant's own menu

`the-great-invention-menu.xlsx` is the real printed menu, transcribed: 146
dishes across 13 sections, at the prices on the board.

```bash
restaurant-ai import-menu menu/the-great-invention-menu.xlsx \
    --allow-uncosted --replace-menu
```

`--replace-menu` retires anything not in the file, so this file *becomes* the
menu — which is what makes the demo dishes go away. `--allow-uncosted` is needed
until the dishes have recipes: the importer otherwise refuses a dish it cannot
cost, and refusing the real menu until every ingredient is priced would keep the
real prices out and leave the demo ones in.

Until recipes exist, these dishes are excluded from margin analysis rather than
counted as costing nothing — a dish with no recipe costs zero, and zero cost is
full margin, which would make every one of them the best-performing item on the
menu.

## Changing it

Edit the Menu sheet and re-import. `sku` is the identity: keep it stable and a
re-import updates the dish in place; change it and you create a new one. Section
names are free text and create sections as needed.

To add a recipe, put the dish's `sku` in the BOM sheet's `parent_code` with one
row per ingredient, and fill in the Ingredients sheet with what those cost. Then
drop `--allow-uncosted` and the importer will prove every dish costs out before
writing anything.
