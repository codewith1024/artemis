"""Final verification: per-screen row accounting + untitled-row forensics."""
import sys
sys.path.insert(0, '/tmp/qnh_xml')
from analyze import load, parse_card, bb, classify, descendants, ZERO

for cat in ('cat_food', 'cat_snacks'):
    screens = load(cat)
    print(f'\n===== {cat}: {len(screens)} screens =====')
    tot_rows = tot_titled = tot_partial = 0
    untitled_detail = []
    for si, sc in enumerate(screens):
        rows = sc['cards']
        titled = [c for c in rows if c['title']]
        un = [c for c in rows if not c['title']]
        tot_rows += len(rows)
        tot_titled += len(titled)
        tot_partial += len(un)
        kinds = []
        for c in un:
            has_img = any((bb(ch) and bb(ch)[0] < 380 and (bb(ch)[2] - bb(ch)[0]) > 50) for ch in [])
            texts = [t for t, b, k in c.get('_texts', [])] if False else None
            kinds.append(f"row_bounds={c['row']} spec={c['spec']!r} prices={c['prices']} "
                         f"nodata={c['nodata']} viewall={c['viewall']}")
        print(f'  {sc["file"]} hdr={sc["header"]} rows={len(rows)} titled={len(titled)} untitled={len(un)}')
        for k in kinds:
            print(f'       UNTITLED {k}')
        if sc['orphans']:
            oset = {o[0] for o in sc['orphans']}
            print(f'       orphans(non-card title-like)={len(sc["orphans"])} texts={sorted(oset)}')
    print(f'  TOTAL rows={tot_rows} titled_rows={tot_titled} untitled_rows={tot_partial}')
