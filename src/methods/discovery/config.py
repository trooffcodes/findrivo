import os

class Config:
    DEBUG = os.getenv("DEBUG", "True").lower() in ("true", "1", "yes")
    HEADLESS = os.getenv("HEADLESS", "True").lower() in ("true", "1", "yes")
    
    CONCURRENCY = int(os.getenv("CONCURRENCY", "2"))
    MAX_LEADS_PER_LOCATION = int(os.getenv("MAX_LEADS", "10"))
    
    RANDOM_DELAY_MIN = float(os.getenv("DELAY_MIN", "0.6"))
    RANDOM_DELAY_MAX = float(os.getenv("DELAY_MAX", "1.4"))
    
    BLOCK_HEAVY_RESOURCES = True
    
    PROGRESS_FILE = os.getenv("PROGRESS_FILE", "discovery_progress.json")
    
    DEFAULT_US_LOCATIONS = ["Austin, TX", "Miami, FL", "Seattle, WA", "Chicago, IL"]
    DEFAULT_NICHES = ["Dental Clinic", "Coffee Roaster", "HVAC Contractor", "Boutique Hotel"]