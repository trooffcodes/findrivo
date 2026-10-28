"""
Adaptive Delays & Anti-Detection Fingerprint Engine
"""

import random
import time
from core.logger import app_logger

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
]

VIEWPORTS = [
    {"width": 1366, "height": 768},
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864}
]


def get_random_user_agent() -> str:
    return random.choice(USER_AGENTS)


def get_random_viewport() -> dict:
    return random.choice(VIEWPORTS)


def simulate_human_scroll(page, min_scrolls=1, max_scrolls=3):
    try:
        scrolls = random.randint(min_scrolls, max_scrolls)
        for _ in range(scrolls):
            distance = random.randint(300, 700)
            page.mouse.wheel(0, distance)
            time.sleep(random.uniform(0.15, 0.4))
    except Exception as e:
        app_logger.debug("Scroll simulation notice: %s", e)


def simulate_human_mouse_movement(page):
    try:
        vp = page.viewport_size or {"width": 1366, "height": 768}
        w, h = vp["width"], vp["height"]
        start_x, start_y = random.randint(50, w - 50), random.randint(50, h - 50)
        end_x, end_y = random.randint(50, w - 50), random.randint(50, h - 50)

        steps = random.randint(4, 7)
        for i in range(steps):
            curr_x = start_x + int((end_x - start_x) * (i / steps))
            curr_y = start_y + int((end_y - start_y) * (i / steps))
            page.mouse.move(curr_x, curr_y)
            time.sleep(random.uniform(0.01, 0.03))
    except Exception as e:
        app_logger.debug("Mouse movement notice: %s", e)


class AdaptivePacingEngine:
    def __init__(self, base_delay_min=1.0, base_delay_max=2.0):
        self.base_delay_min = base_delay_min
        self.base_delay_max = base_delay_max
        self.current_multiplier = 1.0
        self.latency_history = []

    def record_latency(self, latency_seconds: float):
        self.latency_history.append(latency_seconds)
        if len(self.latency_history) > 10:
            self.latency_history.pop(0)

        avg_latency = sum(self.latency_history) / max(len(self.latency_history), 1)
        # The multiplier is capped: without a cap, one slow region compounds
        # into ever-longer delays that can look like a pipeline stall.
        if avg_latency > 4.0:
            self.current_multiplier = min(1.5, self.current_multiplier + 0.2)
            app_logger.warning("High response latency (%.2fs). Multiplier at %.2fx", avg_latency, self.current_multiplier)
        elif avg_latency < 2.0 and self.current_multiplier > 1.0:
            self.current_multiplier = max(1.0, self.current_multiplier - 0.1)

    def get_delay(self) -> float:
        min_d = self.base_delay_min * self.current_multiplier
        max_d = self.base_delay_max * self.current_multiplier
        return random.uniform(min_d, max_d)

    def trigger_captcha_backoff(self):
        pause_sec = random.uniform(25.0, 40.0)
        app_logger.warning("Bot challenge cue detected! Pacing backoff initiated for %.1fs", pause_sec)
        time.sleep(pause_sec)