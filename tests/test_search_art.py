import io
import unittest
from unittest.mock import patch

from PIL import Image
from search_art import poster_strip


class SearchArtTests(unittest.TestCase):
    @staticmethod
    def response(url, **kwargs):
        if url.endswith('/missing'):
            raise OSError('Unavailable')
        colours = ['red', 'green', 'blue', 'yellow', 'purple']
        output = io.BytesIO()
        Image.new('RGB', (228, 342), colours[int(url.rsplit('/', 1)[1])]).save(output, 'PNG')
        return io.BytesIO(output.getvalue())

    def test_five_posters_form_three_above_two_in_result_order(self):
        with patch('search_art.urllib.request.urlopen', side_effect=self.response):
            data = poster_strip(['https://example.org/'+str(i) for i in range(5)], first=6)
        picture = Image.open(io.BytesIO(data))
        self.assertEqual(picture.size, (732, 792))
        # Lower row is centred; pixel samples identify both missing/shifted cards.
        expected = [(120,180,(255,0,0)), (360,180,(0,128,0)), (600,180,(0,0,255)),
                    (240,570,(255,255,0)), (480,570,(128,0,128))]
        for x,y,colour in expected:
            self.assertLess(sum(abs(a-b) for a,b in zip(picture.getpixel((x,y)),colour)),10)

    def test_missing_cover_keeps_its_position_and_all_missing_falls_back(self):
        with patch('search_art.urllib.request.urlopen', side_effect=self.response):
            data = poster_strip(['https://example.org/0','https://example.org/missing',
                                 'https://example.org/2'])
            self.assertIsNone(poster_strip(['https://example.org/missing']))
        picture = Image.open(io.BytesIO(data))
        self.assertEqual(picture.size, (732,402))
        self.assertLess(picture.getpixel((600,180))[0],5)
        self.assertGreater(picture.getpixel((600,180))[2],250)

    def test_single_poster_stays_larger_without_a_tall_full_screen_card(self):
        with patch('search_art.urllib.request.urlopen', side_effect=self.response):
            data = poster_strip(['https://example.org/0'])
        self.assertEqual(Image.open(io.BytesIO(data)).size,(660,402))
