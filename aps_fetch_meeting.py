#!/usr/bin/env python3
"""Fetch an APS meeting: metadata -> events -> presentations -> people -> organizations; event locations."""
import argparse
import gzip
import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
#from aps_export_meeting import build_export

def build_export(data, url=''):
    meeting = data['meeting']
    events = {x['id']: x for x in data['events']}
    presentations = {x['id']: x for x in data['presentations']}
    individuals = {x['id']: x for x in data['individuals']}
    organizations = {x['id']: x for x in data['organizations']}
    locations = {x['id']: x for x in data['locations']}

    def person_label(person_id):
        person = individuals[person_id]
        name = ' '.join(str(person.get(k) or '').strip() for k in ('first_name', 'last_name')).strip()
        affiliations = [organizations[i]['name'] for i in (person.get('affiliation_ids') or [])]
        return f"{name} ({'; '.join(affiliations)})" if affiliations else name

    def location_label(location_id):
        location = locations[location_id]
        return ', '.join(str(location[k]).strip() for k in ('building', 'room') if location.get(k))

    def event_order(event_id):
        starts = [datetime.fromisoformat(period['start'].replace('Z', '+00:00'))
                  for period in (events[event_id].get('periods') or []) if period.get('start')]
        return (min(starts) if starts else datetime.max.replace(tzinfo=timezone.utc), event_id)

    ordered_ids = sorted(events, key=event_order)
    result = {'url': url, 'meeting_id': meeting['id'], 'Year': meeting['meeting_open'][:4], 'name': meeting['title'], 'event': []}
    linked_presentations = set()
    for event_id in ordered_ids:
        event = deepcopy(events[event_id])
        event['location'] = '; '.join(location_label(i) for i in (event.pop('location_ids', None) or []))
        event['presiders'] = [person_label(presider['individual_id'])
                             for presider in (event.get('presiders') or [])
                             if presider.get('individual_id') is not None]
        event['presentation'] = []
        for presentation_id in event.pop('presentation_ids', None) or []:
            presentation = deepcopy(presentations[presentation_id])
            for old_key, new_key in (('author_ids', 'author'), ('presenter_ids', 'presenter')):
                ids = presentation.pop(old_key, None)
                presentation[new_key] = [person_label(i) for i in ids] if ids is not None else []
            event['presentation'].append(presentation)
            linked_presentations.add(presentation_id)
        event['presentation'].sort(key=lambda presentation: (
            datetime.fromisoformat(presentation['start'].replace('Z', '+00:00'))
            if presentation.get('start') else datetime.max.replace(tzinfo=timezone.utc),
            presentation['id'],
        ))
        result['event'].append(event)
    unassigned = set(presentations) - linked_presentations
    if unassigned:
        raise ValueError(f'Presentations have no event; cannot export without omission: {sorted(unassigned)}')
    return result
def decode_json_body(raw):
    """Some APS endpoints return gzip bytes, even without compression negotiation."""
    if raw.startswith(b'\x1f\x8b'):
        raw = gzip.decompress(raw)
    return raw, json.loads(raw)


def write_json(path, data):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('meeting_id', type=int)
    parser.add_argument('--output', type=Path, help='Default: ./aps_meeting_<id>')
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--refresh', action='store_true', help='Redownload all cached source JSON files')
    args = parser.parse_args()
    if args.workers < 1 or args.workers > 32:
        parser.error('--workers must be between 1 and 32')
    root = args.output or Path(f'aps_meeting_{args.meeting_id}')
    root.mkdir(parents=True, exist_ok=True)
    base = f'https://makoshark-data.aps.org/{args.meeting_id}/'
    reports = {}
    started = datetime.now(timezone.utc).isoformat()
    write_json(root / 'pipeline_report.json', {'meeting_id': args.meeting_id, 'started_at': started, 'complete': False})

    def download(remote, dest, expected_id=None, cache=True):
        def validate(data):
            if not isinstance(data, dict):
                raise ValueError(f'Expected JSON object: {remote}')
            if expected_id is not None and data.get('id') != expected_id:
                raise ValueError(f'ID mismatch: {remote}')
            return data
        if cache and not args.refresh and dest.exists():
            try:
                cached = dest.read_bytes()
                raw, data = decode_json_body(cached)
                data = validate(data)
                if raw != cached:
                    tmp = dest.with_suffix(dest.suffix + '.tmp')
                    tmp.write_bytes(raw)
                    tmp.replace(dest)
                return data
            except (ValueError, OSError, EOFError):
                pass
        for attempt in range(5):
            try:
                with urllib.request.urlopen(base + remote, timeout=30) as response:
                    raw = response.read()
                raw, data = decode_json_body(raw)
                data = validate(data)
                tmp = dest.with_suffix(dest.suffix + '.tmp')
                tmp.write_bytes(raw)
                tmp.replace(dest)
                return data
            except (urllib.error.URLError, TimeoutError, ValueError, OSError, EOFError) as exc:
                if attempt == 4:
                    raise
                delay = min(2 ** attempt, 30)
                if isinstance(exc, urllib.error.HTTPError):
                    retry_after = exc.headers.get('Retry-After', '')
                    if retry_after.isdigit():
                        delay = min(max(delay, int(retry_after)), 60)
                time.sleep(delay)

    def batch(kind, ids):
        ids = sorted(set(ids))
        plural = kind + 's'
        directory = root / plural
        directory.mkdir(exist_ok=True)
        records, failures = {}, {}
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            jobs = {pool.submit(download, f'{kind}/{i}.json', directory / f'{i}.json', i): i for i in ids}
            for count, future in enumerate(as_completed(jobs), 1):
                entity_id = jobs[future]
                try:
                    records[entity_id] = future.result()
                except Exception as exc:
                    failures[str(entity_id)] = f'{type(exc).__name__}: {exc}'
                if count % 500 == 0 or count == len(ids):
                    print(f'{plural}: {count}/{len(ids)}, failures={len(failures)}', flush=True)
        report = {'meeting_id': args.meeting_id, 'assembled_at': datetime.now(timezone.utc).isoformat(), 'expected_count': len(ids), 'downloaded_count': len(records), 'complete': not failures, 'failures': failures}
        reports[plural] = report
        write_json(root / f'{kind}_download_report.json', report)
        if failures:
            raise RuntimeError(f'{plural}: {len(failures)} downloads failed; rerun to resume')
        return [records[i] for i in ids]

    try:
        meeting = download('meeting.json', root / 'meeting.json', args.meeting_id)
        index = download('_ndx/meeting/text-meeting.json', root / 'text-meeting.json')
        groups = {'event': set(), 'presentation': set()}
        for raw in index['documentIds'].values():
            entity_id, kind = json.loads(raw)
            if kind in groups:
                groups[kind].add(entity_id)
        write_json(root / 'document_ids.json', {'meeting_id': args.meeting_id, 'source_url': base + '_ndx/meeting/text-meeting.json', 'event_ids': sorted(groups['event']), 'presentation_ids': sorted(groups['presentation'])})
        events = batch('event', groups['event'])
        event_presentations = {i for event in events for i in (event.get('presentation_ids') or [])}
        presentation_ids = groups['presentation'] | event_presentations
        presentations = batch('presentation', presentation_ids)
        author_ids = {i for x in presentations for i in (x.get('author_ids') or [])}
        presenter_ids = {i for x in presentations for i in (x.get('presenter_ids') or [])}
        presider_ids = {chair['individual_id'] for event in events for chair in (event.get('presiders') or []) if chair.get('individual_id') is not None}
        individual_ids = author_ids | presenter_ids | presider_ids
        individuals = batch('individual', individual_ids)
        organization_ids = {i for x in individuals for i in (x.get('affiliation_ids') or [])}
        organizations = batch('organization', organization_ids)
        location_ids = {i for event in events for i in (event.get('location_ids') or [])}
        locations = batch('location', location_ids)
        combined = {'meeting': meeting, 'events': events, 'presentations': presentations, 'individuals': individuals, 'organizations': organizations, 'locations': locations, 'location_collection': reports['locations'], 'collection': reports['presentations'], 'event_collection': reports['events'], 'individual_collection': reports['individuals'], 'organization_collection': reports['organizations']}
        for kind, records, ids in [('event', events, groups['event']), ('presentation', presentations, presentation_ids), ('individual', individuals, individual_ids), ('organization', organizations, organization_ids), ('location', locations, location_ids)]:
            assert len(records) == len(ids) and {x['id'] for x in records} == ids
            info = {'meeting_id': args.meeting_id, f'{kind}_count': len(ids), f'{kind}_ids': sorted(ids)}
            if kind == 'individual':
                info.update(author_count=len(author_ids), presenter_count=len(presenter_ids), presider_count=len(presider_ids))
            write_json(root / f'{kind}_ids.json', info)
            if kind != 'presentation':
                write_json(root / f'meeting_{kind}s.json', {'meeting': meeting, 'collection': reports[kind+'s'], kind+'s': records})
        write_json(root / 'meeting_presentations.json', combined)
        write_json(root / 'download_report.json', reports['presentations'])
        write_json(root / 'meeting_export.json', build_export(combined))
        report = {'meeting_id': args.meeting_id, 'started_at': started, 'completed_at': datetime.now(timezone.utc).isoformat(), 'complete': True, 'stages': reports, 'index_presentation_count': len(groups['presentation']), 'additional_event_presentation_ids': sorted(event_presentations - groups['presentation']), 'author_count': len(author_ids), 'presenter_count': len(presenter_ids), 'presider_count': len(presider_ids), 'presider_only_individual_ids': sorted(presider_ids - author_ids - presenter_ids)}
        write_json(root / 'pipeline_report.json', report)
        print('Complete: ' + ', '.join(f'{k}={v["downloaded_count"]}' for k,v in reports.items()), flush=True)
        print(f'Output: {(root / "meeting_export.json").resolve()}', flush=True)
    except Exception as exc:
        write_json(root / 'pipeline_report.json', {'meeting_id': args.meeting_id, 'started_at': started, 'complete': False, 'stages': reports, 'error': str(exc)})
        raise


if __name__ == '__main__':
    main()
