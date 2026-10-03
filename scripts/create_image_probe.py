#!/usr/bin/env python3
"""Create a fresh local visual marker. Do not paste the answer printed here into ChatGPT."""
from __future__ import annotations
import argparse
from io import BytesIO
import os
from pathlib import Path
import secrets
import string
from PIL import Image, ImageDraw, ImageFont


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New .png file inside your mapped test project; never overwrites')
    args = parser.parse_args()
    if args.output.suffix.lower() != '.png':
        parser.error('Use a new .png output path')
    marker = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
    with Image.new('RGB', (720, 280), 'white') as image:
        draw = ImageDraw.Draw(image)
        draw.text((35, 28), marker, fill='black', font=ImageFont.load_default(size=56))
        draw.rectangle((40, 140, 120, 220), fill='blue')
        draw.ellipse((170, 140, 250, 220), fill='red')
        draw.polygon([(320, 140), (370, 220), (270, 220)], fill='green')
        buf = BytesIO(); image.save(buf, 'PNG')
    try:
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as file:
            file.write(buf.getvalue())
    except OSError:
        parser.exit(1, 'Could not create probe. Use a new filename in an existing authorized directory.\n')
    print(f'Created {args.output}')
    print(f'LOCAL ANSWER ONLY: {marker}; blue square, red circle, green triangle.')
    print('Ask ChatGPT to read the image and identify the contents without giving it this answer.')


if __name__ == '__main__':
    main()
