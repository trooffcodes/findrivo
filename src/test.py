import json
from methods.extraction import BusinessExtractor, PersonalizationEngine

def main():
    target_site = "https://redriverbbq.com/league-city/"

    # Step 1: Extract All Business Contacts & Socials
    print(f"\n[1] Running Lightweight Contact Extraction for {target_site}...")
    extractor = BusinessExtractor(max_pages=5, enrich_socials=True)
    result = extractor.extract(target_site)

    print("\n--- EXTRACTION RESULTS ---")
    print(f"Company: {result.company_name}")
    print(f"Emails Found ({len(result.contacts.emails)}):", result.contacts.emails)
    print(f"Phones Found ({len(result.contacts.phone_numbers)}):", result.contacts.phone_numbers)
    print(f"Addresses Found ({len(result.contacts.addresses)}):", result.contacts.addresses)
    print(f"Contact Forms ({len(result.contacts.contact_forms)}):", result.contacts.contact_forms)
    print(f"Socials Detected ({len(result.contacts.socials)}):")
    for s in result.contacts.socials:
        print(f"  - [{s.platform}] {s.url} (Handle: {s.handle}) | Bio: {s.bio[:60] if s.bio else 'None'}...")

    # Step 2: Personalization Intelligence via LLMClient
    print(f"\n[2] Running Personalization Analysis via LLMClient...")
    engine = PersonalizationEngine()
    brief = engine.analyze_company(target_site, extraction_result=result)

    print("\n--- PERSONALIZATION BRIEF (JSON) ---")
    print(brief.model_dump_json(indent=2))

if __name__ == "__main__":
    main()