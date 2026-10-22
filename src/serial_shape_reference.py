"""Small advisory character-class reference, never a candidate serial answer."""
from collections import defaultdict

from . import asana_client as ac, product_catalog, nvidia_client as vision

ENABLED_ENV = 'JOBSHEET_SERIAL_SHAPE_REFERENCE'
MIN_DISTINCT_DEVICES = 3
MAX_SHAPES = 4


def project(index, product_families):
    if len(product_families) != 1:
        return []
    family = next(iter(product_families))
    groups = defaultdict(set)
    for row in (index or {}).get('devices', []):
        serial = ac._norm(row.get('serial'))
        if row.get('weak_identity') or not vision._valid_serial_token(serial):
            continue
        confirmed = {ac._norm(ac.product_group(p['canonical']))
            for text in row.get('product_variants') or []
            if (p := product_catalog.inspect_product(text))['status'] == 'confirmed'}
        if confirmed != {family}:
            continue
        shape = ''.join('L' if c.isalpha() else 'D' for c in serial)
        groups[shape].add(serial)  # Repeat visits and duplicate rows count once.
    return [{'pattern': shape, 'devices': len(serials)} for shape, serials in
            sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))
            if len(serials) >= MIN_DISTINCT_DEVICES][:MAX_SHAPES]
