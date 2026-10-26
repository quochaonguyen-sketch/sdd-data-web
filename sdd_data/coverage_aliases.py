"""Old/new Zone ID aliases from a versioned, row-aligned coverage catalog."""
from collections import defaultdict
import logging
import re

from .transform import normalized, area_code, operating_area


def alias_coverage(values, duplicate_policy="last"):
    columns = defaultdict(list)
    for index, name in enumerate(values[0]):
        columns[normalized(name)].append(index)
    aliases = defaultdict(list)
    for row in values[1:]:
        def cell(*names):
            return next((str(row[i]).strip() for name in names for i in columns[normalized(name)]
                         if i < len(row) and str(row[i]).strip()), "")
        old, new = cell("Zone ID"), cell("Zone ID v2")
        if not old and not new:
            continue
        district = cell("Quận cũ") or cell("Quận & phường cũ").split("-", 1)[0].strip()
        old_ward, ward = cell("Phường cũ"), cell("Phường mới")
        source = area_code(cell("Khu vực v2", "Khu vực", "Zone"))
        info = {"area": operating_area(district, old_ward, ward, source), "source_area": source,
                "district": district, "old_ward": old_ward, "ward": ward, "canonical_zone": new or old}
        identifiers = {old, new, cell("Zone ID+Hub_Code_NDD")}
        hub = cell("Zone ID+Hub_Code_NDD")
        # The Hub column sometimes corrects a typo in the old Zone column.
        # Keep its explicit base as another alias (never infer from Z prefixes).
        hub_base = re.split(r"-[A-Za-z]\d{2}-", hub, maxsplit=1)[0] if hub else ""
        identifiers.add(hub_base)
        if old and new and normalized(hub).startswith(normalized(old) + "-"):
            identifiers.add(new + hub[len(old):])
        # A trailing .# is a catalog annotation, absent from SPX's Zone ID.
        identifiers.update(key[:-2] for key in tuple(identifiers) if key.endswith(".#"))
        for key in identifiers:
            if not key:
                continue
            key = normalized(key)
            identity = tuple(normalized(info.get(field)) for field in ("area", "district", "old_ward", "ward"))
            if not any(tuple(normalized(candidate.get(field)) for field in ("area", "district", "old_ward", "ward")) == identity for candidate in aliases[key]):
                aliases[key].append(info)
    result = {key: candidates[0] if len(candidates) == 1 else {"zone_candidates": candidates}
              for key, candidates in aliases.items()}
    if not result:
        raise ValueError("CSV không có Zone ID cũ/mới hợp lệ.")
    if duplicate_policy == "error" and any("zone_candidates" in item for item in result.values()):
        raise ValueError("Zone ID cũ/mới có nhiều địa bàn; cần đối chiếu phường/quận người nhận.")
    logging.getLogger(__name__).info("Coverage cũ/mới: %s mã/alias, %s mã cần đối chiếu địa bàn.", len(result), sum("zone_candidates" in item for item in result.values()))
    return result
