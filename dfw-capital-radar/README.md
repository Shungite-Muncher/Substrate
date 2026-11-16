# DFW Capital Event Radar

Interview demo: ranks DFW HUD-insured apartment loans by likelihood of a sale or refi event.
Open `dfw-capital-radar.html` (self-contained, works offline; the ranking is pre-rendered so it reads even where JavaScript is off).

Rebuild (downloads go in a separate folder, see the top of each script):

```
python3 -I build_data.py <dl_dir> hud_dfw_loans.json          # HUD loan + address + portfolio files -> DFW apartment loans
python3 -I match_cad.py hud_dfw_loans.json <TAD .txt> <DCAD dir> cad_matches.json
python3 -I build_html.py hud_dfw_loans.json cad_matches.json   # -> dfw-capital-radar.src.html
NODE_PATH=$(npm root -g) node prerender.js dfw-capital-radar.src.html dfw-capital-radar.html
```

All scoring weights live in the `CONFIG` object at the top of the page script.
