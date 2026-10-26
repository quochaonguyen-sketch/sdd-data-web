from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import logging
import re
import unicodedata

TZ = timezone(timedelta(hours=7))
JOBS = {"pickup": "pickup_volume", "delivery": "delivery_order"}
UNRESOLVED_AREA = "UNRESOLVED"


def normalized(value) -> str:
    text = unicodedata.normalize("NFD", str(value or "").strip().lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn").replace("đ", "d")
    return re.sub(r"\s+", " ", text)


def area_code(value) -> str | None:
    match = re.fullmatch(r"(?:(?:khu vuc|khu|kv|area|zone)\s*)?([1-6])", normalized(value).replace("_", " ").replace("-", " "))
    return f"KV{match[1]}" if match else None


def location_key(value):
    return re.sub(r"^(?:thanh pho|tp\.?|quan|q\.?|huyen|h\.?|phuong|p\.?|xa)\s*", "", normalized(value)).strip()


def operating_area(district, old_ward, new_ward, source_area):
    """User-approved five-area geography; Coverage SA's Zone column is legacy."""
    district = location_key(district)
    ward = location_key(old_ward) or location_key(new_ward)
    fixed = {
        "10": "KV1", "11": "KV1", "5": "KV1", "6": "KV1", "8": "KV1",
        "4": "KV2", "7": "KV2", "binh thanh": "KV2", "2": "KV2", "9": "KV2",
        "binh tan": "KV3", "tan phu": "KV3",
        "1": "KV4", "3": "KV4", "tan binh": "KV4", "phu nhuan": "KV4",
        "hoc mon": "KV5", "12": "KV5", "go vap": "KV5",
    }
    if district in fixed:
        return fixed[district]
    if district == "binh chanh":
        return "KV1" if ward in {"binh hung", "phong phu"} else "KV3" if ward else None
    if district == "thu duc":
        former_thu_duc = {"hiep binh chanh", "hiep binh phuoc", "linh dong", "linh tay",
                          "linh trung", "linh xuan", "binh chieu", "tam binh", "tam phu",
                          "binh tho", "linh chieu", "truong tho"}
        former_q2_q9 = {"an khanh", "an loi dong", "an phu", "binh an", "binh khanh",
                       "binh trung dong", "binh trung tay", "cat lai", "thanh my loi", "thao dien",
                       "thu thiem", "phuoc binh", "phuoc long a", "phuoc long b", "hiep phu",
                       "long binh", "long phuoc", "long thanh my", "long truong", "phu huu",
                       "tan phu", "tang nhon phu a", "tang nhon phu b", "truong thanh"}
        if ward in former_thu_duc:
            return "KV5"
        if ward in former_q2_q9:
            return "KV2"
        # New ward names can span old boundaries: do not guess from the legacy KV.
        return None
    return source_area


def as_datetime(value) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        if number <= 0:
            return None
        return datetime.fromtimestamp(number, TZ)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def cot_label(kind: str, dt: datetime) -> str:
    if kind == "delivery":
        cot = "COT 1.1" if dt.time() < time(8) else "COT 1.2" if dt.time() < time(13) else "COT 2"
        day = dt.date()
    else:
        day = dt.date() + (timedelta(days=1) if dt.time() >= time(15, 30) else timedelta())
        cot = "COT 2" if time(11, 15) <= dt.time() < time(15, 30) else "COT 1"
    return f"{day.isoformat()} {cot}"


def coverage_map(values: list[list[str]], duplicate_policy="last") -> dict:
    if duplicate_policy not in ("last", "error"):
        raise ValueError("Duplicate policy phải là last hoặc error.")
    if not values:
        raise ValueError("Coverage SA trống.")
    names = {normalized(name): i for i, name in enumerate(values[0])}
    if "zone id v2" in names:
        from .coverage_aliases import alias_coverage
        return alias_coverage(values, duplicate_policy)
    if "zone id" not in names or not ({"zone", "khu vuc"} & names.keys()):
        raise ValueError("Coverage SA phải có cột Zone ID và Zone.")
    result, conflicts = {}, set()
    for row in values[1:]:
        def cell(name):
            index = names.get(normalized(name), len(row))
            return str(row[index]).strip() if index < len(row) else ""
        zone = normalized(cell("Zone ID"))
        if not zone:
            continue
        info = {
            "area": area_code(cell("Zone") or cell("Khu vực")), "ward": cell("Phường mới"),
            "old_ward": cell("Phường cũ"),
            "district": cell("Quận & phường cũ").split("-", 1)[0].strip(),
        }
        info["source_area"] = info["area"]
        info["area"] = operating_area(info["district"], info["old_ward"], info["ward"], info["source_area"])
        if zone in result and result[zone] != info:
            if duplicate_policy == "error":
                raise ValueError(f"Zone ID bị trùng với mapping khác nhau: {zone}")
            conflicts.add(zone)
        result[zone] = info
    if not result:
        raise ValueError("Không có Zone ID hợp lệ trong mapping.")
    if conflicts:
        logging.getLogger(__name__).warning("Coverage SA có %s Zone ID trùng mapping; dùng dòng cuối giống launcher2. Kiểm tra tab nguồn nếu cần thay đổi.", len(conflicts))
    return result


def match_zone(zone: str, mapping: dict, *, kind="delivery", ward_hint="", district_hint=""):
    """Exact match, then longest parent prefix at a hyphen boundary."""
    zone = normalized(zone)
    def resolve(key):
        info = mapping[key]
        if "zone_candidates" not in info:
            return key, info
        candidates = info["zone_candidates"]
        if ward_hint:
            candidates = [item for item in candidates if location_key(ward_hint) in {location_key(item.get("ward")), location_key(item.get("old_ward"))}]
        if district_hint:
            candidates = [item for item in candidates if location_key(district_hint) == location_key(item.get("district"))]
        if len(candidates) == 1:
            return key, candidates[0]
        # Delivery uses the former ward. A renamed merged ward can differ
        # without changing the delivery geography; pickup must keep it unresolved.
        if kind == "delivery" and candidates and len({(item.get("area"), item.get("district"), item.get("old_ward")) for item in candidates}) == 1:
            return key, {**candidates[0], "ward": ""}
        return "", {}
    if zone in mapping:
        return resolve(zone)
    candidates = [key for key in mapping if zone.startswith(key + "-")]
    if candidates:
        key = max(candidates, key=len)
        return resolve(key)
    return "", {}


def confirmed_pickup_location(route, ward, mapping, mapping_districts=None, district_candidates=None):
    """Confirmed prefixes, numbered districts and unique catalog abbreviations."""
    prefix, separator, detail = normalized(route).partition("_")
    aliases = {"bt": "Quận Bình Tân", "tp": "Quận Tân Phú", "bth": "Quận Bình Thạnh"}
    district = aliases.get(prefix) if separator else None
    if separator and not district:
        number = re.fullmatch(r"q(0?[1-9]|1[0-2])", prefix)
        if number:
            district = f"Quận {int(number[1])}"
        else:
            names = mapping_districts if mapping_districts is not None else {item.get("district") for item in mapping.values() if item.get("district")}
            matches = {name for name in names if prefix == "".join(word[0] for word in location_key(name).split())}
            if len(matches) == 1:
                district = next(iter(matches))
    known_ward = location_key(ward) not in {"", "unknown", "chua ro phuong/xa", "chua xac dinh phuong/xa"}
    if not district and known_ward and location_key(ward) == "binh hung hoa":
        district = "Quận Bình Tân"
    if not district:
        return None
    d_key = location_key(district)
    candidates = district_candidates.get(d_key, []) if district_candidates is not None else [info for info in mapping.values() if location_key(info.get("district")) == d_key]
    result_ward = ward if known_ward else "Chưa rõ phường/xã"
    if not known_ward and separator:
        ward_token = detail.split("_", 1)[0].strip()
        numbered_ward = re.fullmatch(r"p(\d{1,2})(?:\d{2})?", ward_token)
        stem = re.sub(r"[\d\s]+$", "", ward_token).strip()
        def matches(name):
            name = location_key(name)
            if numbered_ward and name.isdigit():
                return int(name) == int(numbered_ward[1])
            abbreviation = "".join(word[0] for word in name.split())
            return bool(name) and (stem == name or stem.replace(" ", "") in {abbreviation, "p" + abbreviation})
        wards = {item.get("ward") for item in candidates if matches(item.get("old_ward")) or matches(item.get("ward"))}
        wards.discard(None)
        wards.discard("")
        if len(wards) == 1:
            result_ward = next(iter(wards))
    area = operating_area(district, "", result_ward if known_ward or result_ward != "Chưa rõ phường/xã" else "", None)
    if not area:
        areas = {item.get("area") for item in candidates}
        area = next(iter(areas)) if len(areas) == 1 else None
    return {"area": area or UNRESOLVED_AREA, "district": district, "ward": result_ward}


def summarize(kind: str, orders: list[dict], mapping: dict, report_date: str, areas: tuple[str, ...], seller_wards=None, unresolved_orders=None):
    if kind not in JOBS:
        raise ValueError("Chỉ hỗ trợ pickup hoặc delivery.")
    selected = date.fromisoformat(report_date)
    stats = Counter(input=len(orders))
    groups, seen, unmapped_zones = {}, set(), Counter()
    keys = ("seller_zone_id", "pickup_zone_id", "sender_zone_id") if kind == "pickup" else ("zone_id", "receiver_zone_id", "pickup_zone_id", "buyer_zone_id")
    seller_wards = seller_wards or {}
    seller_locations = {}
    seller_old_locations = {}
    mapping_districts = set()
    district_candidates = {}
    for info in mapping.values():
        seller_locations.setdefault(location_key(info.get("ward")), []).append(info)
        seller_old_locations.setdefault(location_key(info.get("old_ward")), []).append(info)
        d = info.get("district")
        if d:
            mapping_districts.add(d)
            district_candidates.setdefault(location_key(d), []).append(info)
    for order in orders:
        seller_ward = ""
        shipment = str(order.get("shipment_id") or "").strip()
        if not shipment:
            stats["invalid_id"] += 1
            continue
        if shipment in seen:
            stats["duplicates"] += 1
            continue
        seen.add(shipment)
        value = next((order.get(key) for key in ("create_time", "ctime", "createTime") if order.get(key)), None) if kind == "pickup" else order.get("current_station_received_time")
        dt = as_datetime(value)
        if dt is None:
            stats["invalid_time"] += 1
            continue
        if dt.date() != selected:
            stats["outside_date"] += 1
            continue
        pickup_route = str(order.get("pickup_route_name") or "").strip() if kind == "pickup" else ""
        zone = pickup_route or next((str(order.get(key)).strip() for key in keys if order.get(key) not in (None, "")), "")
        receiver_ward = seller_wards.get(str(order.get("lowest_buyer_address_id") or "")) or order.get("lowest_buyer_address_name") or order.get("receiver_ward_name") or ""
        matched, info = match_zone(zone, mapping, kind=kind,
            ward_hint=receiver_ward if kind == "delivery" else "",
            district_hint=order.get("buyer_district_name", "") if kind == "delivery" else "")
        if kind == "pickup" and not matched:
            seller_id = str(order.get("lowest_seller_address_id") or "").strip()
            seller_ward = seller_wards.get(seller_id) or order.get("lowest_seller_address_name") or order.get("seller_ward_name") or ""
            seller_district = order.get("seller_district_name") or order.get("seller_addr_district") or ""
            # Uploaded catalog contains new wards. A new ward with the same name
            # as a former ward elsewhere must not pull in the former location.
            catalog_ward = seller_id in seller_wards
            candidates = seller_locations.get(location_key(seller_ward), [])
            if not catalog_ward and not candidates:
                candidates = seller_old_locations.get(location_key(seller_ward), [])
            if seller_district:
                candidates = [item for item in candidates if location_key(item.get("district")) == location_key(seller_district)]
            choices = {(item.get("area"), item.get("district"), item.get("ward")): item for item in candidates}
            if len(choices) == 1:
                info = next(iter(choices.values()))
                matched = "seller_address"
                stats["seller_address_matched"] += 1
                if not zone:
                    stats["missing_seller_route"] += 1
            elif choices and len({item.get("area") for item in choices.values()}) == 1 and all(item.get("area") for item in choices.values()):
                # A merged ward may span former districts while remaining
                # wholly inside one operating area. Preserve that known scope.
                info = {"area": next(iter(choices.values()))["area"],
                        "district": "Chưa rõ quận cũ", "ward": seller_ward}
                matched = "seller_area"
                stats["seller_area_matched"] += 1
            else:
                stats["unmapped_seller_address"] += 1
        if kind == "pickup":
            confirmed = confirmed_pickup_location(zone, seller_ward or info.get("ward"), mapping, mapping_districts, district_candidates)
            if confirmed:
                info = confirmed
                matched = "confirmed_pickup_route"
                stats["confirmed_pickup_location"] += 1
        if not matched or not info.get("area"):
            stats["unmapped"] += 1
            missing_label = f"seller_address_id:{order.get('lowest_seller_address_id') or '(trống)'}" if kind == "pickup" else "(trống)"
            unmapped_zones[zone or missing_label] += 1
            if kind != "pickup":
                if unresolved_orders is not None:
                    unresolved_orders.append({key: order.get(key) for key in ("shipment_id", "zone_id", "receiver_zone_id", "buyer_zone_id", "current_station_received_time", "lowest_buyer_address_id", "lowest_buyer_address_name", "buyer_district_name") if order.get(key) is not None})
                continue
            stats["unresolved"] += 1
            if unresolved_orders is not None:
                unresolved_orders.append({"shipment_id": shipment,
                    "lowest_seller_address_id": str(order.get("lowest_seller_address_id") or ""),
                    "seller_ward": seller_ward, "seller_zone_id": zone or None,
                    "pickup_point_id": order.get("pickup_point_id") or None,
                    "pickup_route_name": pickup_route or None,
                    "create_time": value, "cot": cot_label(kind, dt), "status": "unresolved",
                    "reason": "Chưa xác định duy nhất địa bàn seller; cần fetch/đối chiếu lại"})
            info = {"area": UNRESOLVED_AREA, "district": "Chưa rõ địa bàn seller",
                    "ward": seller_ward or "Chưa rõ phường/xã"}
        if info["area"] not in areas and not (kind == "pickup" and info["area"] == UNRESOLVED_AREA):
            stats["outside_area"] += 1
            continue
        if info.get("source_area") and info["source_area"] != info["area"]:
            stats["remapped_area"] += 1
        ward_key = "new_ward" if kind == "pickup" else "old_ward"
        ward = info.get("ward" if kind == "pickup" else "old_ward") or info.get("old_ward") or "UNKNOWN"
        district = info.get("district") or "UNKNOWN"
        cot = cot_label(kind, dt)
        route = normalized(zone) if kind == "pickup" else ""
        identity = "|".join((kind, report_date, info["area"], cot, normalized(district), normalized(ward), route))
        summary_id = "sdd:" + hashlib.sha256(identity.encode()).hexdigest()[:40]
        if summary_id not in groups:
            groups[summary_id] = {
                "summary_id": summary_id, "report_date": report_date, ward_key: ward,
                "district": district, "area": info["area"], "cot": cot, "total_orders": 0,
            }
            if kind == "pickup":
                groups[summary_id]["ma_tuyen"] = zone or None
        groups[summary_id]["total_orders"] += 1
        stats["included"] += 1
        if kind == "pickup" and info["area"] != UNRESOLVED_AREA:
            stats["resolved"] += 1
    stats["summary_rows"] = len(groups)
    details = dict(stats)
    if unmapped_zones:
        details["unmapped_zones"] = dict(unmapped_zones)
    return sorted(groups.values(), key=lambda row: row["summary_id"]), details
