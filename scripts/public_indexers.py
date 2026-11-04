"""Test public English movie/TV indexers with a durable, ownership-scoped undo."""
import copy
import datetime
import json
import re
import uuid
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


def probe_feed(indexer_id, search_type, query):
    xml = ET.parse('/var/lib/prowlarr/config.xml').getroot()
    base = xml.findtext('UrlBase') or ''
    params = urllib.parse.urlencode({'t': search_type, 'q': query, 'cat': '2000', 'limit': 100})
    url = f'http://127.0.0.1:9696{base}/{int(indexer_id)}/api?{params}'
    request = urllib.request.Request(url, headers={'X-Api-Key': xml.findtext('ApiKey')})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            root = ET.fromstring(response.read())
    except urllib.error.HTTPError as error:
        return {'error': 'HTTP ' + str(error.code)}
    except (urllib.error.URLError, TimeoutError, ET.ParseError):
        return {'error': 'Connection timeout/failure or invalid XML response'}
    error = next((n for n in root.iter() if n.tag.split('}')[-1] == 'error'), None)
    if error is not None:
        return {'error': 'Torznab error code ' + str(error.get('code', 'unknown'))}
    rows = []
    for item in root.findall('.//item'):
        attrs = {n.get('name'): n.get('value') for n in item.iter()
                 if n.tag.split('}')[-1] == 'attr'}
        rows.append({'title': item.findtext('title') or '', 'seeders': attrs.get('seeders', '?')})
    return {'rows': rows}


def diagnose(api, probe=probe_feed):
    print('READ-ONLY diagnostic: no settings changes, grabs or downloads.', flush=True)
    indexers = api('radarr', 'indexer')
    statuses = {s['indexerId']: s for s in api('radarr', 'indexerstatus')}
    print('\nRadarr sources:', flush=True)
    for r in indexers:
        status = statuses.get(r['id'], {})
        print(f"  {r['name']}: automatic={r.get('enableAutomaticSearch')} interactive={r.get('enableInteractiveSearch')} disabledUntil={status.get('disabledTill', 'none')}", flush=True)
        cats = next((f.get('value') for f in r.get('fields', []) if f['name'] == 'categories'), [])
        remove_year = next((f.get('value') for f in r.get('fields', []) if f['name'] == 'removeYear'), None)
        print(f'    categories={cats}; removeYear={remove_year}', flush=True)
    targets = [r for r in api('prowlarr', 'indexer')
               if str(r.get('definitionName', '')).lower() == 'thepiratebay'
               or r.get('name', '').startswith('The Pirate Bay')]
    if not targets:
        print('No Pirate Bay indexer is configured in Prowlarr.')
        return
    for target in targets:
        print('\nProwlarr movie-category probes: ' + target['name'], flush=True)
        for search_type, query in [('search', 'Insidious'), ('movie', 'Insidious'),
                                   ('movie', 'Insidious 2010'), ('movie', 'Insidious 2011')]:
            print(f'  Testing type={search_type}, query={query!r} ...', flush=True)
            result = probe(target['id'], search_type, query)
            if 'error' in result:
                print('    ' + result['error'], flush=True)
                continue
            rows = result['rows']
            uhd = [r for r in rows if re.search(r'2160|\b4k\b', r['title'], re.I)]
            print(f'    Results={len(rows)}; labelled 4K={len(uhd)}', flush=True)
            for row in (uhd[:6] or rows[:3]):
                # Movie titles only. Never print feed links, raw errors or API keys.
                title = re.sub(r'https?://\S+', '[link removed]', row['title'])
                title = re.sub(r'[\x00-\x1f\x7f]', ' ', title)[:240]
                print('    ' + title + ' | reported seeders=' + str(row['seeders']), flush=True)

# Category labels alone are insufficient: adult/anime definitions sometimes map
# their entire catalogue to Movies or TV. Unknown definitions fail closed.
GENERAL_SOURCES = {
    '1337x', 'thepiratebay', 'limetorrents', 'yts', 'eztv', 'showrss',
    'torrentdownload', 'torrentdownloads', 'kickasstorrentsto',
    'kickasstorrentsws', 'extratorrentst',
}


def general_source(resource):
    name = re.sub(r'[^a-z0-9]', '', resource.get('name', '').lower())
    return name in GENERAL_SOURCES


def categories(nodes):
    result = set()
    for node in nodes or []:
        result.add(int(node['id']))
        result.update(categories(node.get('subCategories')))
    return result


def allowed_category(value):
    return 2000 <= int(value) < 3000 or 5000 <= int(value) < 6000


def candidate(resource):
    return (general_source(resource)
            and str(resource.get('privacy', '')).lower() == 'public'
            and str(resource.get('protocol', '')).lower() == 'torrent'
            and str(resource.get('language', '')).lower().split('-')[0] == 'en'
            and resource.get('supportsSearch') is True
            and any(allowed_category(c) for c in categories(
                (resource.get('capabilities') or {}).get('categories'))))


def identity(resource):
    return (resource.get('implementation'), resource.get('definitionName') or resource.get('name'))


def signature(resource):
    keys = ('name', 'implementation', 'configContract', 'appProfileId', 'enable',
            'redirect', 'priority', 'downloadClientId', 'protocol', 'privacy', 'tags')
    result = {key: resource.get(key) for key in keys}
    result['fields'] = sorted([(f['name'], f.get('value')) for f in resource.get('fields', [])])
    return json.dumps(result, sort_keys=True)


def operate(cfg, api, atomic, own, undo=False):
    path = Path(cfg['home']) / '.local/state/media-request-bot/public-indexers.json'
    state = json.loads(path.read_text()) if path.exists() else None

    def save():
        atomic(path, json.dumps(state, indent=2) + '\n')
        own(path, cfg)

    def current():
        return api('prowlarr', 'indexer')

    def sync():
        try:
            api('prowlarr', 'command', {'name': 'ApplicationIndexerSync'}, 'POST')
            print('Application indexer sync queued. Check Radarr Settings > Indexers after it finishes.')
        except Exception:
            print('Sync could not be queued. Use Prowlarr Settings > Apps > Sync App Indexers.')

    if undo:
        if not state or state.get('finishedUndo'):
            print('No recorded public indexer additions to undo.')
            return
        # A pending POST may have succeeded even if its response was lost.
        pending = state.get('pending')
        if pending:
            matches = [r for r in current() if r['name'] == pending['name']
                       and identity(r) == tuple(pending['identity'])]
            if len(matches) > 1:
                raise ValueError('Ambiguous pending indexer. No deletion performed.')
            if matches and matches[0]['id'] not in state['baseline']:
                r = matches[0]
                state['added'].append({'id': r['id'], 'signature': signature(r)})
            state['pending'] = None
            save()
        for entry in state['added']:
            found = next((r for r in current() if r['id'] == entry['id']), None)
            if found is None:
                continue
            if found['id'] in state['baseline'] or signature(found) != entry['signature']:
                raise ValueError('An added indexer was edited or replaced. Refusing to delete it; undo can be retried after review.')
            # Knaben and every other baseline indexer are protected by ID.
            api('prowlarr', 'indexer/' + str(found['id']), method='DELETE')
            print('Removed: ' + found['name'])
        profile = next((p for p in api('prowlarr', 'appprofile')
                        if p['name'] == state['profileName']), None)
        if profile:
            expected = {'enableRss': False, 'enableAutomaticSearch': True,
                        'enableInteractiveSearch': True, 'minimumSeeders': 1}
            if any(profile.get(k) != v for k, v in expected.items()):
                raise ValueError('Helper profile was edited. Leaving it intact for review.')
            if any(r.get('appProfileId') == profile['id'] for r in current()):
                raise ValueError('Helper profile is still in use. Leaving it intact.')
            api('prowlarr', 'appprofile/' + str(profile['id']), method='DELETE')
        state['finishedUndo'] = True
        save()
        sync()
        print('Undo complete. All indexers present before this helper were preserved, including Knaben.')
        return

    if state and not state.get('finishedUndo'):
        print('This batch is already recorded. Run media-server indexers-undo before starting another batch.')
        return
    existing = current()
    # Only movie/TV categories may be synced by the existing application connections.
    # Do not silently change connections or the categories used by existing indexers.
    apps = api('prowlarr', 'applications')
    enabled_apps = [a for a in apps if str(a.get('syncLevel', '')).lower() not in ('disabled', '')]
    if not enabled_apps:
        raise ValueError('No enabled Prowlarr app connection. Configure Radarr in Settings > Apps first.')
    for app in enabled_apps:
        cats = next((f.get('value') for f in app.get('fields', []) if f['name'] == 'syncCategories'), [])
        if not isinstance(cats, list) or not cats or not all(allowed_category(c) for c in cats):
            raise ValueError('An app connection syncs categories outside Movies/TV. Set its Sync Categories to Movies/TV only before running this helper. No settings changed.')
    schemas = api('prowlarr', 'indexer/schema')
    known = {identity(r) for r in existing}
    names = {r['name'].casefold() for r in existing}
    options = [r for r in schemas if candidate(r) and identity(r) not in known
               and r['name'].casefold() not in names]
    marker = uuid.uuid4().hex[:10]
    state = {'created': datetime.datetime.now(datetime.timezone.utc).isoformat(),
             'baseline': [r['id'] for r in existing], 'added': [], 'pending': None,
             'profileName': 'Media Server Public ' + marker, 'finishedUndo': False}
    save()
    profile = api('prowlarr', 'appprofile', {'name': state['profileName'],
                  'enableRss': False, 'enableAutomaticSearch': True,
                  'enableInteractiveSearch': True, 'minimumSeeders': 1}, 'POST')
    print(f'Testing {len(options)} selected public English movie/TV sources. RSS is disabled for these additions.', flush=True)
    print('Only the reviewed general movie/TV source list is eligible; adult/anime specialists are excluded.', flush=True)
    for source in options:
        if identity(source) in known:
            continue
        payload = copy.deepcopy(source)
        payload.pop('id', None)
        payload.update(name=source['name'] + ' [MS-' + marker + ']', enable=True,
                       appProfileId=profile['id'], priority=25, tags=[], downloadClientId=0)
        print('Testing: ' + source['name'], flush=True)
        try:
            api('prowlarr', 'indexer/test', payload, 'POST')
        except Exception as error:
            # Expose only a status code, never raw responses, URLs or credentials.
            status = re.search(r'HTTP (\d{3})', str(error))
            suffix = ' (HTTP ' + status.group(1) + ')' if status else ''
            print('Skipped: test failed' + suffix + '. See Prowlarr System > Events for the reason.', flush=True)
            continue
        state['pending'] = {'name': payload['name'], 'identity': list(identity(payload))}
        save()
        try:
            added = api('prowlarr', 'indexer', payload, 'POST')
        except Exception:
            raise ValueError('Add did not return a confirmed result. Stopped safely; run media-server indexers-undo to reconcile and undo this batch.') from None
        state['added'].append({'id': added['id'], 'name': source['name'], 'signature': signature(added)})
        state['pending'] = None
        save()
        known.add(identity(source))
        print('Added: ' + source['name'], flush=True)
    sync()
    print(f"Added {len(state['added'])} sources. Existing indexers and quality rules preserved.")
    print('\nSources added by this batch:')
    for entry in state['added']:
        print('  - ' + entry['name'])
    if not state['added']:
        print('  None. All candidates were already configured or failed testing.')
    print('Undo this batch: media-server indexers-undo')
    print('English is the source language; individual releases may contain other audio languages.')
