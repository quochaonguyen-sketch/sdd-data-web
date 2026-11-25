"""Lossless dictionary encoding for the private central pickup publisher."""
GROUP_FIELDS = ('pickup_station_id', 'pickup_station_name', 'pickup_point_id',
                'pickup_point_name', 'pickup_point_district', 'district', 'full_address',
                'cot_group', 'zone_name', 'assigned_riders_today', 'area', 'ward')
RECORD_FIELDS = ('tracking_id', 'shipment_id', 'sls_tracking_number', 'create_time')


def compact_pickup_payload(payload):
    groups, indices, records = [], {}, []
    for row in payload['p_rows']:
        status = row.get('status')
        if status not in ('Assigned', 'Created'):
            raise ValueError('Only Assigned and Created pickup orders may be published')
        group = tuple(row.get(key) for key in GROUP_FIELDS)
        if group not in indices:
            indices[group] = len(groups)
            groups.append(group)
        record = [*(row.get(key) for key in RECORD_FIELDS), indices[group]]
        if status == 'Created':
            record.append(status)
        records.append(record)
    return {**{key: value for key, value in payload.items() if key != 'p_rows'},
            'p_records': records, 'p_groups': groups}
