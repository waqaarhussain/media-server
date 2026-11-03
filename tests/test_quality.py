import copy
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_manage import manage


class DolbyQualityTests(unittest.TestCase):
    def test_repeated_apply_preserves_filters_other_scores_and_creates_only_two_formats(self):
        with tempfile.TemporaryDirectory() as home:
            config = Path(home) / '.config/media-request-bot'
            config.mkdir(parents=True)
            (config / 'radarr.env').write_text('RADARR_PROFILE_ID=7\n')
            profile = {'id': 7, 'name': '4K Balanced', 'upgradeAllowed': False,
                       'minFormatScore': 0, 'items': [{'quality': {'id': 19}, 'allowed': True}],
                       'formatItems': [{'format': 8, 'name': 'Size rejection', 'score': -10000},
                                       {'format': 9, 'name': 'HDR', 'score': 50}]}
            original = copy.deepcopy(profile)
            formats = []
            writes = []

            def api(app, endpoint, payload=None, method=None):
                self.assertEqual(app, 'radarr')
                if endpoint == 'customformat/schema':
                    return [{'implementation': 'ReleaseTitleSpecification', 'fields': [{'name': 'value'}]}]
                if endpoint == 'customformat':
                    if method == 'POST':
                        resource = dict(copy.deepcopy(payload), id=100+len(formats))
                        formats.append(resource)
                        return copy.deepcopy(resource)
                    return copy.deepcopy(formats)
                self.assertEqual(endpoint, 'qualityprofile/7')
                if method == 'PUT':
                    writes.append(copy.deepcopy(payload))
                    profile.clear()
                    profile.update(copy.deepcopy(payload))
                return copy.deepcopy(profile)

            with patch.object(manage, 'api', side_effect=api), patch.object(manage, 'own'), patch('builtins.print'):
                manage.dolby_quality({'home': home})
                manage.dolby_quality({'home': home})
            self.assertEqual(len(formats), 2)
            self.assertEqual(len(writes), 1)
            self.assertEqual(profile['items'], original['items'])
            self.assertEqual(profile['minFormatScore'], 0)
            self.assertFalse(profile['upgradeAllowed'])
            self.assertEqual(profile['formatItems'][:2], original['formatItems'])
            self.assertEqual([i['score'] for i in profile['formatItems'][2:]], [100, 100])
            self.assertTrue(list((Path(home)/'.local/state/media-request-bot/quality-backups').glob('*.json')))

    def test_dolby_labels_match_dot_dash_and_underscore_but_not_words_containing_labels(self):
        vision, atmos = [pattern for _, pattern in manage.DOLBY_FORMATS]
        for title in ('Movie.2160p.DV.Atmos', 'Movie_2160p_DoVi_Atmos', 'Movie-Dolby-Vision-Atmos'):
            self.assertIsNotNone(re.search(vision, title, re.I))
            self.assertIsNotNone(re.search(atmos, title, re.I))
        for title in ('Movie.DVD.Atmospheric', 'Movie.ADVision.Atmosphere'):
            self.assertIsNone(re.search(vision, title, re.I))
            self.assertIsNone(re.search(atmos, title, re.I))


if __name__ == '__main__':
    unittest.main()
