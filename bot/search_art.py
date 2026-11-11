"""Build one compact, numbered strip from the current search result posters."""
import io
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageDraw, ImageFont, ImageOps


def poster_strip(urls, first=1):
    if not any(urls):
        return None

    def fetch(url):
        if not url or not url.startswith('https://'):
            return None
        try:
            with urllib.request.urlopen(url, timeout=6) as response:
                data = response.read(5 * 1024 * 1024 + 1)
            if len(data)>5 * 1024 * 1024:
                return None
            with Image.open(io.BytesIO(data)) as picture:
                if picture.width * picture.height > 20_000_000:
                    return None
                return ImageOps.fit(picture.convert('RGB'), (150, 225))
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=5) as workers:
        posters = list(workers.map(fetch, urls[:5]))
    if not any(poster is not None for poster in posters):
        return None
    width = max(660, len(posters)*162+12)
    strip = Image.new('RGB', (width, 270), '#20232c')
    draw = ImageDraw.Draw(strip)
    try:
        font = ImageFont.truetype('DejaVuSans.ttf', 22)
    except OSError:
        font = ImageFont.load_default()
    for offset, poster in enumerate(posters):
        x = (width-len(posters)*162)//2+6+offset*162
        if poster is not None:
            strip.paste(poster, (x, 12))
        else:
            draw.rectangle((x, 12, x+149, 236), fill='#383c47')
            draw.text((x+15, 100), 'No cover', font=font, fill='white')
        draw.text((x+65, 240), str(first+offset), font=font, fill='white')
    output = io.BytesIO()
    strip.save(output, format='JPEG', quality=88)
    return output.getvalue()
