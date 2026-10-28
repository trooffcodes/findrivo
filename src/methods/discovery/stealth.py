"""
Playwright Stealth & Performance Hardening Module
"""

from playwright.sync_api import Page, BrowserContext, Route
from config import Config
from core.logger import app_logger

try:
    from playwright_stealth import stealth_sync
    HAS_PLAYWRIGHT_STEALTH = True
except ImportError:
    HAS_PLAYWRIGHT_STEALTH = False

STEALTH_CHROMIUM_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-infobars",
    "--window-position=0,0",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--hide-scrollbars",
    "--mute-audio",
]

# Disabling the Chromium sandbox is unsafe when rendering untrusted pages. It is
# opt-in only for constrained container environments where no other option works.
if Config.PLAYWRIGHT_NO_SANDBOX:
    app_logger.warning("Chromium sandbox is disabled via PLAYWRIGHT_NO_SANDBOX.")
    STEALTH_CHROMIUM_ARGS.extend(["--no-sandbox", "--disable-setuid-sandbox"])

EVASION_SCRIPT = """
(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined, configurable: true });

    if (!window.chrome) { window.chrome = {}; }
    window.chrome.runtime = {
        PlatformOs: { MAC: 'mac', WIN: 'win', ANDROID: 'android', LINUX: 'linux' },
        PlatformArch: { ARM: 'arm', X86_64: 'x86-64' },
        connect: function() {},
        sendMessage: function() {}
    };
    window.chrome.loadTimes = function() {};
    window.chrome.csi = function() {};

    const fakePlugins = [
        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' }
    ];
    Object.defineProperty(navigator, 'plugins', { get: () => fakePlugins, configurable: true });

    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications' ?
            Promise.resolve({ state: Notification.permission }) :
            originalQuery(parameters)
    );

    const getParameterProto = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {
        if (param === 37445) return 'Google Inc. (Apple)';
        if (param === 37446) return 'ANGLE (Apple, Apple M1 Pro, OpenGL 4.1)';
        return getParameterProto.apply(this, [param]);
    };
})();
"""


def route_filter(route: Route):
    """Speed optimization: Aborts heavy images, media, and fonts."""
    if route.request.resource_type in ["image", "media", "font"]:
        route.abort()
    else:
        route.continue_()


def apply_stealth_and_speed(context: BrowserContext, page: Page, block_resources: bool = True):
    """Enables both stealth scripts and network route optimization."""
    context.add_init_script(EVASION_SCRIPT)
    if HAS_PLAYWRIGHT_STEALTH:
        try:
            stealth_sync(page)
        except Exception:
            pass

    if block_resources:
        # Route aborting saves ~60% network wait time
        page.route("**/*", route_filter)