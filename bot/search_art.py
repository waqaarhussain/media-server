"""Build a larger, numbered grid from the current search result posters."""
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
                return ImageOps.fit(picture.convert('RGB'), (228, 342))
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=5) as workers:
        posters = list(workers.map(fetch, urls[:5]))
    if not any(poster is not None for poster in posters):
        return None
    columns = min(3, len(posters))
    rows = (len(posters)+columns-1)//columns
    cell_width, row_height = 240, 390
    width = max(660, columns*cell_width+12)
    strip = Image.new('RGB', (width, rows*row_height+12), '#20232c')
    draw = ImageDraw.Draw(strip)
    try:
        font = ImageFont.truetype('DejaVuSans.ttf', 26)
    except OSError:
        font = ImageFont.load_default()
    for offset, poster in enumerate(posters):
        row, column = divmod(offset, columns)
        count = min(columns, len(posters)-row*columns)
        x = (width-count*cell_width)//2+6+column*cell_width
        y = 12+row*row_height
        if poster is not None:
            strip.paste(poster, (x, y))
        else:
            draw.rectangle((x, y, x+227, y+341), fill='#383c47')
            draw.text((x+50, y+155), 'No cover', font=font, fill='white')
        label = str(first+offset)
        bounds = draw.textbbox((0, 0), label, font=font)
        draw.text((x+(228-(bounds[2]-bounds[0]))//2, y+350), label, font=font, fill='white')
    output = io.BytesIO()
    strip.save(output, format='JPEG', quality=88)
    return output.getvalue()
