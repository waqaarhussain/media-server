"""Enable/undo the local verified-year search adapter without altering movie records."""
import json
import secrets
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

NAME = 'Media Server Verified Years'
BASE = 'http://127.0.0.1:18787'


def owned(resource, config):
    fields = {f['name']: f.get('value') for f in resource.get('fields', [])}
    return (resource.get('name') == NAME and resource.get('implementation') == 'Torznab'
            and fields.get('baseUrl', '').rstrip('/') == BASE
            and fields.get('apiKey') == config['adapterKey'])


def operate(cfg, manage, undo=False):
    path = Path(cfg['home']) / '.config/media-request-bot/year-adapter.json'
    config = json.loads(path.read_text()) if path.exists() else None
    existing = manage.api('radarr', 'indexer')
    if undo:
        if config is None:
            print('Verified-year adapter is not enabled.')
            return
        matches = [r for r in existing if owned(r, config)]
        if len(matches) > 1:
            raise ValueError('Multiple adapter connections exist. Review them in Radarr before undo.')
        if config.get('indexerId'):
            recorded = next((r for r in existing if r['id'] == config['indexerId']), None)
            if recorded and not owned(recorded, config):
                raise ValueError('Adapter connection was edited/replaced. Refusing to delete it.')
        for resource in matches:
            manage.api('radarr', 'indexer/' + str(resource['id']), method='DELETE')
        path.unlink()
        manage.run(['systemctl', 'restart', 'media-request-bot.service'])
        print('Verified-year adapter removed. Original indexers, movies and quality rules preserved.')
        return
    if config is None:
        if any(r.get('name') == NAME for r in existing):
            raise ValueError('An unowned connection with the adapter name exists. Review it before setup.')
        bot_config = manage.env_read(Path(cfg['home']) / '.config/media-request-bot/radarr.env')
        xml = ET.parse('/var/lib/prowlarr/config.xml').getroot()
        config = {'adapterKey': secrets.token_urlsafe(32),
                  'radarrUrl': bot_config['RADARR_URL'], 'radarrKey': bot_config['RADARR_API_KEY'],
                  'prowlarrUrl': 'http://127.0.0.1:9696' + (xml.findtext('UrlBase') or ''),
                  'prowlarrKey': xml.findtext('ApiKey')}
        manage.atomic(path, json.dumps(config, indent=2) + '\n')
        manage.own(path, cfg)
    matches = [r for r in existing if owned(r, config)]
    if len(matches) > 1 or (config.get('indexerId') and any(
            r['id'] == config['indexerId'] and not owned(r, config) for r in existing)):
        raise ValueError('Adapter identity changed. Review the connection before continuing.')
    manage.deploy_bot(cfg)
    # Verify the local authenticated listener before creating a Radarr connection.
    ready = False
    for _ in range(20):
        try:
            request = urllib.request.Request(BASE + '/api?t=caps', headers={'X-Api-Key': config['adapterKey']})
            with urllib.request.urlopen(request, timeout=2) as response:
                ready = b'Media Server Verified Years' in response.read()
            if ready:
                break
        except Exception:
            time.sleep(0.5)
    if not ready:
        raise ValueError('Local adapter did not start. Bot remains available; check its service log. Run media-server years-undo to disable the adapter.')
    if matches:
        print('Verified-year adapter already configured. No duplicate added.')
        return
    schema = next(r for r in manage.api('radarr', 'indexer/schema') if r['implementation'] == 'Torznab')
    payload = {k: v for k, v in schema.items() if k != 'id'}
    payload.update(name=NAME, enableRss=False, enableAutomaticSearch=True,
                   enableInteractiveSearch=True, priority=25, tags=[])
    values = {'baseUrl': BASE, 'apiPath': '/api', 'apiKey': config['adapterKey'],
              'categories': [2000], 'minimumSeeders': 1, 'removeYear': False}
    for field in payload['fields']:
        if field['name'] in values:
            field['value'] = values[field['name']]
    resource = manage.api('radarr', 'indexer', payload, 'POST')
    config['indexerId'] = resource['id']
    manage.atomic(path, json.dumps(config, indent=2) + '\n')
    manage.own(path, cfg)
    print('Verified-year adapter enabled. No movie metadata or quality rules changed; no download started.')
    print('Search the movie again in Telegram. Undo only this adapter: media-server years-undo')
