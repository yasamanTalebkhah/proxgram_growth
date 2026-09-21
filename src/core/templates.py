import re
import random
from typing import List


class SpintaxEngine:
    @staticmethod
    def spin(text: str) -> str:
        # Only braced groups containing at least one "|" are spintax choices;
        # plain {variable} placeholders are left intact for render_promo.
        pattern = re.compile(r"\{([^{}|]+\|[^{}]+)\}")
        while True:
            match = pattern.search(text)
            if not match:
                break
            choices = match.group(1).split("|")
            text = text[:match.start()] + random.choice(choices) + text[match.end():]
        return text

    @staticmethod
    def render_promo(template: str, channel_link: str, extra_data: dict = None) -> str:
        spun_text = SpintaxEngine.spin(template)
        data = dict(extra_data or {})
        data["channel_link"] = channel_link
        for key, val in data.items():
            spun_text = spun_text.replace(f"{{{key}}}", str(val))
        return spun_text
