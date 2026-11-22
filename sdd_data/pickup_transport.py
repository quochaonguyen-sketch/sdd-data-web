"""Lossless dictionary encoding for the private central pickup publisher."""
GROUP_FIELDS = ('pickup_station_id', 'pickup_station_name', 'pickup_point_id',
                'pickup_point_name', 'pickup_point_district', 'district', 'full_address',
                'cot_group', 'zone_name', 'assigned_riders_today', 'area', 'ward')
RECORD_FIELDS = ('tracking_id', 'shipment_id', 'sls_tracking_number', 'create_time')


def compact_pickup_payload(payload):
    groups, indices, records = [], {}, []
    for row in payload['p_rows']:
        if row.get('status') != 'Assigned':
            raise ValueError('Only Assigned pickup orders may be published')
        group = tuple(row.get(key) for key in GROUP_FIELDS)
        if group not in indices:
            indices[group] = len(groups)
            groups.append(group)
        records.append([*(row.get(key) for key in RECORD_FIELDS), indices[group]])
    return {**{key: value for key, value in payload.items() if key != 'p_rows'},
            'p_records': records, 'p_groups': groups}
